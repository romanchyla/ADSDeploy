from ADSDeploy.pipeline.generic import RabbitMQWorker
from ADSDeploy import osutils, app
from ADSDeploy.models import KeyValue
import os
import time
import threading


def create_executioner(payload):
    """From the information in the payload, create the Executioner."""
    assert 'application' in payload
    assert 'environment' in payload
    
    if not os.path.exists(os.path.abspath(app.config.get("EB_DEPLOY_HOME"))):
            raise Exception("The EB_DEPLOY_HOME is invalid")
        
    # identify the eb-deploy application home, usually root/<name>/<name>
    app_home = os.path.join(app.config.get('EB_DEPLOY_HOME'), payload['application'], 
                            payload['application'])
    
    if not os.path.exists(app_home):
        raise Exception('The {0} does not exist'.format(app_home))
    
    pyenv = app.config.get('EB_DEPLOY_VIRTUALENV', 
                         os.path.join(app.config.get('EB_DEPLOY_HOME'), 'python/bin/activate'))
    if not os.path.exists(pyenv):
        raise Exception('The EB_DEPLOY_VIRTUALENV is invalid')
    
    return osutils.Executioner(pyenv, app_home, app.config.get('MAX_WAIT_TIME', 30*60))


def is_timedout(payload, timestamp_key='timestamp'):
    """Make sure we are not running longer than MAX_WAIT_TIME."""
    if time.time() - payload.get(timestamp_key, time.time()) > app.config.get('MAX_WAIT_TIME', 30*60):
        return True
    return False


class BeforeDeploy(RabbitMQWorker):
    """Checks the environment before running the deployment. If the environment
    is in the 'pending' state, it will keep waiting MAX_WAIT_TIME.
    """
    
    def process_payload(self, payload, 
        channel=None, 
        method_frame=None, 
        header_frame=None):
        """
        Receives information about the environment that
        is about to de deployed
        """
        
        if is_timedout(payload, timestamp_key='init_timestamp'):
            payload['err'] = 'timeout'
            payload['msg'] = 'BeforeDeploy: waiting too long for the environment to come up'
            payload['deployed'] = False

            self.publish(payload, topic=self.params['status'])

            return self.publish_to_error_queue(payload,
                                               header_frame=header_frame)
        
        x = create_executioner(payload)
        
        # checks we can access the AWS and that the environment in question
        # is not busy
        r = x.cmd("./find-env-by-attr url {0}".format(payload['environment']))
        self.logger.info(r.retcode)
        assert r.retcode == 0
        
        for l in r.out.splitlines():
            parts = l.split()

            # the environment is not ready, we have to wait
            if len(parts) > 1 and parts[0] != 'Ready':
                
                # re-publish the payload to the queue,
                # but do not block the worker
                def run(payload, worker):
                    if not 'init_timestamp' in payload:
                        payload['init_timestamp'] = time.time()
                    worker.publish(payload, topic=self.subscribe_topic)
                return threading.Timer(30, run, args=[payload, self]).start()
        
        action = payload.get('action', 'deploy')
        
        if action == 'deploy':
            payload['msg'] = 'OK to deploy'
            self.publish(payload, topic='ads.deploy.deploy')
            self.publish(payload, topic=self.params['status'])
        elif action.startswith('restart'):
            payload['msg'] = 'Deploy to be restarted'
            self.publish(payload, topic='ads.deploy.restart')
            self.publish(payload, topic=self.params['status'])
        else:
            raise Exception('Unknown action {0}'.format(action))


class Deploy(RabbitMQWorker):
    """
    A wrapper around the eb-deploy's safe-deploy.sh script.
    We'll just execute the deployment and wait MAX_WAIT_TIME.
    On success, publish the payload. On failure, send it to
    the error queue.
    """
      
    def process_payload(self, payload, 
        channel=None, 
        method_frame=None, 
        header_frame=None):
        """Runs the actual deployment. It calls the eb-deploy safe-deploy.sh."""
        
        x = create_executioner(payload)
        payload['msg'] = '{0}-{1} deployment starts'\
            .format(payload['environment'], payload['application'])
        self.publish(payload, topic=self.params['status'])

        # this will run for a few minutes!
        r = x.cmd('./safe-deploy.sh {0} > /tmp/deploy.{0}.{1}'
                  .format(payload['environment'], payload['application']))
        if r.retcode == 0:
            payload['deployed'] = True
            payload['msg'] = 'deployed'
            self.publish(payload)
            self.publish(payload, topic=self.params['status'])
        else:
            payload['err'] = 'deployment failed'
            payload['deployed'] = False
            payload['msg'] = 'deployment failed; command: {0}, reason: {1}, ' \
                             'stdout: {2}'.format(r.command, r.err, r.out)

            self.publish_to_error_queue(payload, header_frame=header_frame)
            self.publish(payload, topic=self.params['status'])


class Restart(RabbitMQWorker):
    """
    This will set a new value into the RESTARTED variable. Effectively
    causing the reload of the machine(s)
    """
      
    def process_payload(self, payload, 
        channel=None, 
        method_frame=None, 
        header_frame=None):
        """It will restart the machine or tha applicaiton."""
        
        x = create_executioner(payload)
        action = payload.get('action', 'restart-soft')
        
        payload['msg'] = '{0}-{1} {2} starts'.format(payload['environment'], 
                                                     payload['application'],
                                                     action)
        self.publish(payload, topic=self.params['status'])
        
        r = None
        if action == 'restart-soft':
            r = x.cmd('./restart-soft {0}'.format(payload['environment']))
        elif action == 'restart-hard':
            r = x.cmd('./restart-hard {0}'.format(payload['environment']))
        else:
            self.publish_to_error_queue(payload)
            
        if r and r.retcode == 0:
            payload['msg'] = 'restart succeeded'
            self.publish(payload)
            self.publish(payload, topic=self.params['status'])
        else:
            payload['msg'] = str(r)
            self.publish_to_error_queue(payload, header_frame=header_frame)
            self.publish(payload, topic=self.params['status'])
        
            
class AfterDeploy(RabbitMQWorker):
    """After the deployment was finished, clean up the 
    AWS instances."""
    
    def process_payload(self, payload, 
        channel=None, 
        method_frame=None, 
        header_frame=None):
        """Runs the cleanup after the deployment happened."""
        
        # reset the timer
        key = '{0}.{1}.last-used'.format(payload['application'], payload['environment'])
        now = time.time()
        with app.session_scope() as session:
            u = session.query(KeyValue).filter_by(key=key).first()
            if u is not None:
                u.value = now
            u = KeyValue(key=key, value=now)
            session.add(u)
            session.commit()
