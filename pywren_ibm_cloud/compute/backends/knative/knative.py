import os
import sys
import json
import time
import yaml
import logging
import requests
import http.client
from urllib.parse import urlparse
from kubernetes import client, config, watch
from pywren_ibm_cloud.utils import version_str
from pywren_ibm_cloud.version import __version__
from . import config as kconfig

import urllib3
urllib3.disable_warnings()
logging.getLogger('kubernetes').setLevel(logging.CRITICAL)
logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)

#Monkey patch for issue: https://github.com/kubernetes-client/python/issues/895
from kubernetes.client.models.v1_container_image import V1ContainerImage
def names(self, names):
    self._names = names
V1ContainerImage.names = V1ContainerImage.names.setter(names)

logger = logging.getLogger(__name__)


class KnativeServingBackend:
    """
    A wrap-up around Knative Serving APIs.
    """

    def __init__(self, knative_config):
        self.log_level = os.getenv('PYWREN_LOGLEVEL')
        self.name = 'knative'
        self.knative_config = knative_config
        self.endpoint = self.knative_config.get('endpoint')
        self.service_hosts = {}

        # k8s config can be in ~/.kube/config or generate kube-config.yml file and
        # set env variable KUBECONFIG=<path-to-kube-confg>
        config.load_kube_config()
        self.api = client.CustomObjectsApi()
        self.v1 = client.CoreV1Api()
        # get current context
        current_context = config.list_kube_config_contexts()[1]
        current_context_details = current_context.get('context')
        # get namespace of current context
        self.namespace = current_context_details.get('namespace', 'default')
        # get the cluster
        self.cluster = current_context_details.get('cluster')
        self.headers = {'content-type': 'application/json'}

        if self.endpoint is None:
            try:
                ingress = self.v1.read_namespaced_service('istio-ingressgateway', 'istio-system')
                http_port = list(filter(lambda port: port.port == 80, ingress.spec.ports))[0].node_port
                https_port = list(filter(lambda port: port.port == 443, ingress.spec.ports))[0].node_port

                if ingress.status.load_balancer.ingress is not None:
                    # get loadbalancer ip
                    ip = ingress.status.load_balancer.ingress[0].ip
                else:
                    # for minikube or a baremetal cluster that has no external load balancer
                    node = self.v1.list_node()
                    ip = node.items[0].status.addresses[0].address

                self.endpoint = 'http://{}:{}'.format(ip, http_port)

            except Exception as e:
                log_msg = "Something went wrong getting the istio-ingressgateway endpoint: {}".format(e)
                logger.info(log_msg)

        log_msg = 'PyWren v{} init for Knative Serving - Endpoint: {}'.format(__version__, self.endpoint)
        logger.info(log_msg)
        if not self.log_level:
            print(log_msg)
        logger.debug('Knative Serving init for endpoint: {}'.format(self.endpoint))

    def _format_service_name(self, runtime_name, runtime_memory):
        runtime_name = runtime_name.replace('/', '--').replace(':', '--')
        return '{}--{}mb'.format(runtime_name, runtime_memory)

    def _unformat_service_name(self, service_name):
        runtime_name, memory = service_name.rsplit('--', 1)
        image_name = runtime_name.replace('--', '/', 1)
        image_name = image_name.replace('--', ':', -1)
        return image_name, int(memory.replace('mb', ''))

    def _get_default_runtime_image_name(self):
        docker_user = self.knative_config['docker_user']
        this_version_str = version_str(sys.version_info)
        if this_version_str == '3.5':
            image_name = kconfig.RUNTIME_DEFAULT_35
        elif this_version_str == '3.6':
            image_name = kconfig.RUNTIME_DEFAULT_36
        elif this_version_str == '3.7':
            image_name = kconfig.RUNTIME_DEFAULT_37
        return image_name.replace('<USER>', docker_user)

    def _get_service_host(self, service_name):
        """
        gets the service host needed for the invocation
        """
        # Check local cache
        if service_name in self.service_hosts:
            return self.service_hosts[service_name]
        else:
            try:
                svc = self.api.get_namespaced_custom_object(
                            group="serving.knative.dev",
                            version="v1alpha1",
                            name=service_name,
                            namespace=self.namespace,
                            plural="services"
                    )
                if svc is not None:
                    service_host = svc['status']['url'][7:]
                else:
                    raise Exception('Unable to get service details from {}'.format(service_name))
            except Exception as e:
                if json.loads(e.body)['code'] == 404:
                    log_msg = 'Knative service: resource "{}" Not Found'.format(service_name)
                    raise(log_msg)
                else:
                    raise(e)

            self.service_hosts[service_name] = service_host

            return service_host

    def _create_account_resources(self):
        """
        Creates the secret to access to the docker hub and the ServiceAcount
        """
        logger.debug("Creating Account resources: Secret and ServiceAccount")
        string_data = {'username': self.knative_config['docker_user'],
                       'password': self.knative_config['docker_token']}
        secret_res = yaml.safe_load(kconfig.secret_res)
        secret_res['stringData'] = string_data

        if self.knative_config['docker_repo'] != kconfig.DOCKER_REPO_DEFAULT:
            secret_res['metadata']['annotations']['tekton.dev/docker-0'] = self.knative_config['docker_repo']

        account_res = yaml.safe_load(kconfig.account_res)
        secret_res_name = secret_res['metadata']['name']
        account_res_name = account_res['metadata']['name']

        try:
            self.v1.delete_namespaced_secret(secret_res_name, self.namespace)
            self.v1.delete_namespaced_service_account(account_res_name, self.namespace)
        except Exception:
            # account resource Not Found - Not deleted
            pass

        self.v1.create_namespaced_secret(self.namespace, secret_res)
        self.v1.create_namespaced_service_account(self.namespace, account_res)

    def _create_build_resources(self):
        logger.debug("Creating Build resources: PipelineResource and Task")
        git_res = yaml.safe_load(kconfig.git_res)
        git_res_name = git_res['metadata']['name']

        task_def = yaml.safe_load(kconfig.task_def)
        task_name = task_def['metadata']['name']

        if 'git_url' in self.knative_config:
            git_url_param = {'name': 'url', 'value': self.knative_config['git_url']}
        else:
            git_url_param = {'name': 'url', 'value': kconfig.GIT_URL_DEFAULT}

        if 'git_rev' in self.knative_config:
            git_rev_param = {'name': 'revision', 'value': self.knative_config['git_rev']}
        else:
            revision = 'master' if 'SNAPSHOT' in __version__ else __version__
            git_rev_param = {'name': 'revision', 'value': revision}

        params = [git_url_param, git_rev_param]
        git_res['spec']['params'] = params

        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_name,
                    namespace=self.namespace,
                    plural="tasks",
                    body=client.V1DeleteOptions()
                )
        except Exception:
            # ksvc resource Not Found  - Not deleted
            pass

        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=git_res_name,
                    namespace=self.namespace,
                    plural="pipelineresources",
                    body=client.V1DeleteOptions()
                )
        except Exception:
            # ksvc resource Not Found - Not deleted
            pass

        self.api.create_namespaced_custom_object(
                group="tekton.dev",
                version="v1alpha1",
                namespace=self.namespace,
                plural="pipelineresources",
                body=git_res
            )

        self.api.create_namespaced_custom_object(
                group="tekton.dev",
                version="v1alpha1",
                namespace=self.namespace,
                plural="tasks",
                body=task_def
            )

    def _build_docker_image_from_git(self, docker_image_name):
        """
        Builds the docker image and pushes it to the docker container registry
        """
        logger.debug("Building default docker image from git")

        revision = 'latest' if 'SNAPSHOT' in __version__ else __version__

        if self.knative_config['docker_repo'] == 'docker.io' and revision != 'latest':
            resp = requests.get('https://index.docker.io/v1/repositories/{}/tags/{}'
                                .format(docker_image_name, revision))
            if resp.status_code == 200:
                logger.debug('Docker image docker.io/{}:{} already created in Dockerhub. '
                             'Skipping build process.'.format(docker_image_name, revision))
                return

        task_run = yaml.safe_load(kconfig.task_run)
        image_url = {'name': 'imageUrl', 'value': '/'.join([self.knative_config['docker_repo'], docker_image_name])}
        task_run['spec']['inputs']['params'].append(image_url)
        image_tag = {'name': 'imageTag', 'value':  revision}
        task_run['spec']['inputs']['params'].append(image_tag)

        self._create_account_resources()
        self._create_build_resources()

        task_run_name = task_run['metadata']['name']
        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_run_name,
                    namespace=self.namespace,
                    plural="taskruns",
                    body=client.V1DeleteOptions()
                )
        except Exception:
            pass

        self.api.create_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    namespace=self.namespace,
                    plural="taskruns",
                    body=task_run
                )

        logger.debug("Building image...")
        pod_name = None
        w = watch.Watch()
        for event in w.stream(self.api.list_namespaced_custom_object, namespace=self.namespace,
                              group="tekton.dev", version="v1alpha1", plural="taskruns",
                              field_selector="metadata.name={0}".format(task_run_name)):
            if event['object'].get('status') is not None:
                pod_name = event['object']['status']['podName']
                w.stop()

        if pod_name is None:
            raise Exception('Unable to get the pod name from the task that is building the runtime')

        w = watch.Watch()
        for event in w.stream(self.v1.list_namespaced_pod, namespace=self.namespace,
                              field_selector="metadata.name={0}".format(pod_name)):
            if event['object'].status.phase == "Succeeded":
                w.stop()
            if event['object'].status.phase == "Failed":
                w.stop()
                raise Exception('Unable to create the Docker image from the git repository')

        self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_run_name,
                    namespace=self.namespace,
                    plural="taskruns",
                    body=client.V1DeleteOptions()
                )

        logger.debug('Docker image created from git and uploaded to Dockerhub')

    def _create_service(self, docker_image_name, runtime_memory, timeout):
        """
        Creates a service in knative based on the docker_image_name and the memory provided
        """
        logger.debug("Creating PyWren runtime service resource in k8s")
        svc_res = yaml.safe_load(kconfig.service_res)

        revision = 'latest' if 'SNAPSHOT' in __version__ else __version__
        # TODO: Take into account revision in service name
        service_name = self._format_service_name(docker_image_name, runtime_memory)
        svc_res['metadata']['name'] = service_name
        svc_res['metadata']['namespace'] = self.namespace

        svc_res['spec']['template']['spec']['timeoutSeconds'] = timeout
        docker_image = '/'.join([self.knative_config['docker_repo'], docker_image_name])
        svc_res['spec']['template']['spec']['container']['image'] = '{}:{}'.format(docker_image, revision)
        svc_res['spec']['template']['spec']['container']['resources']['limits']['memory'] = '{}Mi'.format(runtime_memory)

        try:
            # delete the service resource if exists
            self.api.delete_namespaced_custom_object(
                    group="serving.knative.dev",
                    version="v1alpha1",
                    name=service_name,
                    namespace=self.namespace,
                    plural="services",
                    body=client.V1DeleteOptions()
                )
        except Exception:
            pass

        # create the service resource
        self.api.create_namespaced_custom_object(
                group="serving.knative.dev",
                version="v1alpha1",
                namespace=self.namespace,
                plural="services",
                body=svc_res
            )

        w = watch.Watch()
        for event in w.stream(self.api.list_namespaced_custom_object,
                              namespace=self.namespace, group="serving.knative.dev",
                              version="v1alpha1", plural="services",
                              field_selector="metadata.name={0}".format(service_name)):
            conditions = None
            if event['object'].get('status') is not None:
                conditions = event['object']['status']['conditions']
                if event['object']['status'].get('url') is not None:
                    service_url = event['object']['status']['url']
            if conditions and conditions[0]['status'] == 'True' and \
               conditions[1]['status'] == 'True' and conditions[2]['status'] == 'True':
                # Workaround to prevent invoking the service immediately after creation.
                # TODO: Open issue.
                time.sleep(1)
                w.stop()

        log_msg = 'Runtime Service resource created - URL: {}'.format(service_url)
        logger.debug(log_msg)

        self.service_hosts[service_name] = service_url[7:]

        return service_url

    def _generate_runtime_meta(self, docker_image_name, memory):
        """
        Extract installed Python modules from docker image
        """
        payload = {}

        payload['service_route'] = "/preinstalls"
        logger.debug("Extracting Python modules list from: {}".format(docker_image_name))
        try:
            runtime_meta = self.invoke(docker_image_name, memory, payload, return_result=True)
        except Exception as e:
            raise Exception("Unable to invoke 'modules' action {}".format(e))

        if not runtime_meta or 'preinstalls' not in runtime_meta:
            raise Exception('Failed getting runtime metadata: {}'.format(runtime_meta))

        return runtime_meta

    def create_runtime(self, docker_image_name, memory, timeout=kconfig.RUNTIME_TIMEOUT_DEFAULT):
        """
        Creates a new runtime into the knative default namespace from an already built Docker image.
        As knative does not have a default image already published in a docker registry, pywren
        has to build it in the docker hub account provided by the user. So when the runtime docker
        image name is not provided by the user in the config, pywren will build the default from git.
        """
        default_runtime_img_name = self._get_default_runtime_image_name()
        if docker_image_name in ['default', default_runtime_img_name]:
            # We only build the default image. rest of images must already exist
            # in the docker registry.
            docker_image_name = default_runtime_img_name
            self._build_docker_image_from_git(default_runtime_img_name)

        service_url = self._create_service(docker_image_name, memory, timeout)

        if self.endpoint is None:
            self.endpoint = service_url

        runtime_meta = self._generate_runtime_meta(docker_image_name, memory)

        return runtime_meta

    def build_runtime(self, docker_image_name, dockerfile):
        """
        Builds a new runtime from a Docker file and pushes it to the Docker hub
        """
        logger.info('Building a new docker image from Dockerfile')
        logger.info('Docker image name: {}'.format(docker_image_name))

        if dockerfile:
            cmd = 'docker build -t {} -f {} .'.format(docker_image_name, dockerfile)
        else:
            cmd = 'docker build -t {} .'.format(docker_image_name)

        res = os.system(cmd)
        if res != 0:
            exit()

        cmd = 'docker push {}'.format(docker_image_name)
        res = os.system(cmd)
        if res != 0:
            exit()

    def delete_runtime(self, docker_image_name, memory):
        service_name = self._format_service_name(docker_image_name, memory)
        logger.info('Deleting runtime: {}'.format(service_name))
        try:
            self.api.delete_namespaced_custom_object(
                    group="serving.knative.dev",
                    version="v1alpha1",
                    name=service_name,
                    namespace=self.namespace,
                    plural="services",
                    body=client.V1DeleteOptions()
                )
        except Exception:
            pass

    def delete_all_runtimes(self):
        """
        Deletes all runtimes deployed in knative
        """
        runtimes = self.list_runtimes()
        for docker_image_name, memory in runtimes:
            self.delete_runtime(docker_image_name, memory)

    def list_runtimes(self, docker_image_name='all'):
        """
        List all the runtimes deployed in knative
        return: list of tuples [docker_image_name, memory]
        """
        knative_services = self.api.list_namespaced_custom_object(
                                group="serving.knative.dev",
                                version="v1alpha1",
                                namespace=self.namespace,
                                plural="services"
                            )
        runtimes = []

        for service in knative_services['items']:
            try:
                if service['spec']['template']['metadata']['labels']['type'] == 'pywren-runtime':
                    runtime_name = service['metadata']['name']
                    image_name, memory = self._unformat_service_name(runtime_name)
                    if docker_image_name == image_name or docker_image_name == 'all':
                        runtimes.append((image_name, memory))
            except Exception:
                # It is not a pywren runtime
                pass

        return runtimes

    def invoke(self, docker_image_name, memory, payload, return_result=False):
        """
        Invoke -- return information about this invocation
        """
        service_name = self._format_service_name(docker_image_name, memory)

        self.headers['Host'] = self._get_service_host(service_name)
        if self.endpoint is None:
            self.endpoint = 'http://{}'.format(self._get_service_host(service_name))
        exec_id = payload.get('executor_id', '')
        call_id = payload.get('call_id', '')
        job_id = payload.get('job_id', '')
        route = payload.get("service_route", '/')

        try:
            logger.debug('ExecutorID {} | JobID {} - Starting function invocation {}'
                         .format(exec_id, job_id, call_id))
            start = time.time()
            parsed_url = urlparse(self.endpoint)
            conn = http.client.HTTPConnection(parsed_url.netloc, timeout=600)
            conn.request("POST", route,
                         body=json.dumps(payload),
                         headers=self.headers)
            logger.debug('ExecutorID {} | JobID {} - Function invocation {} done. Waiting '
                         'for a response'.format(exec_id, job_id, call_id))
            resp = conn.getresponse()
            resp_status = resp.status
            resp_data = resp.read().decode("utf-8")
            conn.close()
            roundtrip = time.time() - start
            resp_time = format(round(roundtrip, 3), '.3f')

            if resp_status in [200, 202]:
                data = json.loads(resp_data)
                log_msg = ('ExecutorID {} | JobID {} - Function activation {} finished! ({}s) '
                           .format(exec_id, job_id, call_id, resp_time))
                logger.debug(log_msg)
                if return_result:
                    return data
                return data["activationId"]
            elif resp_status == 404:
                raise Exception("PyWren runtime is not deployed in your k8s cluster")
            else:
                log_msg = ('ExecutorID {} | JobID {} - Function invocation {} failed: {} {}'
                           .format(exec_id, job_id, call_id, resp_status, resp_data))
                logger.debug(log_msg)

        except Exception as e:
            conn.close()
            log_msg = ('ExecutorID {} | JobID {} - Function invocation {} failed: {}'
                       .format(exec_id, job_id, call_id, str(e)))
            logger.debug(log_msg)

    def get_runtime_key(self, docker_image_name, runtime_memory):
        """
        Method that creates and returns the runtime key.
        Runtime keys are used to uniquely identify runtimes within the storage,
        in order to know which runtimes are installed and which not.
        """
        service_name = self._format_service_name(docker_image_name, runtime_memory)
        cluster_key = urlparse(self.cluster).netloc if urlparse(self.cluster).netloc != '' \
                          else urlparse(self.cluster).path
        runtime_key = os.path.join(cluster_key, self.namespace, service_name)

        return runtime_key
