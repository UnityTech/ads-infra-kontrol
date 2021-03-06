import json
import logging
import os
import string
import struct
import time
import statsd

from kontrol.fsm import Aborted, FSM, MSG
from kontrol.main import outgoing
from math import floor
from os.path import isfile
from socket import inet_aton


#: our ochopod logger
logger = logging.getLogger('kontrol')


class Actor(FSM):

    """
    Actor emitting a periodic HTTP POST request against the controlling party. This enables us
    to report relevant information about the pod. The pod UUID is derived from its IPv4 address
    and launch time shortened via base 62 encoding. A random nonce is added to the payload to make
    sure a pod with a given ip being restarted (or at least the kontrol process) triggers a
    digest change.

    @note the IP retrieved from the K8S API at boot time appears to be missing depending on timing
    """

    tag = 'keepalive'

    def __init__(self, cfg, target):

        super(Actor, self).__init__()

        self.cfg = cfg
        self.data.last = 0
        self.data.next = 0
        self.key = '%s' % self._shorten(struct.unpack("!I", inet_aton(cfg['ip']))[0])
        self.nonce = os.urandom(8).encode('hex')
        self.path = '%s actor (%s)' % (self.tag, target)
        self.payload = ''
        self.state = 'up'
        self.statsd = statsd.StatsClient('127.0.0.1', 8125)
        self.target = target
        
        logger.info('%s : now using key %s (pod %s)' % (self.path, self.key, cfg['id']))

    def reset(self, data):

        if self.terminate:
            super(Actor, self).reset(data)

        return 'initial', data, 0.0

    def initial(self, data):
        
        #
        # - $KONTROL_PAYLOAD is optional and can be set to point to a file
        #   on disk that contains json user-data (for instance some statistics)
        # - this free-form payload will be included in the keepalive HTTP PUT,
        #   persisted in etcd and made available to the callback script
        # - stat the file and force a keepalive if it changed
        # - silently skip any error
        #
        # @todo monitor the payload file and force a keepalive upon update
        #
        force = False
        if 'payload' in self.cfg:
                try:
                    updated = os.stat(self.cfg['payload']).st_mtime
                    if updated > data.last:
                        data.last = updated
                        logger.debug('%s : loading %s' % (self.path, self.cfg['payload']))
                        with open(self.cfg['payload'], 'r') as f:
                            self.payload = json.loads(f.read())
                            force = True
            
                except (IOError, OSError, ValueError):
                    pass

        now = time.time()
        if self.terminate or force or now > data.next:

            #
            # - assemble the payload that will be reported periodically to the masters
            #   via the keepalive /PUT request
            #
            js = \
            {
                'app': self.cfg['labels']['app'],
                'id': self.cfg['id'],
                'ip': self.cfg['ip'],
                'key': self.key,
                'nonce': self.nonce,
                'payload': self.payload,
                'role': self.cfg['labels']['role']
            }

            #
            # - if we are going down force a keepalive and set the down trigger
            # - this allows the leader to gracefully skim this pod 
            #
            if self.terminate:
                js['down'] = True

            #
            # - simply HTTP PUT our cfg with a 1 second timeout
            # - the ping frequency is once every TTL * 0.75 seconds
            # - please note any failure to post will be handled with exponential backoff by
            #   the state-machine
            #
            # @todo use TLS
            #
            ttl = int(self.cfg['ttl'])
            payload = (self.target, json.dumps(js, sort_keys=True))
            logger.debug('%s : ping @ %s' % (self.path, self.target))
            outgoing.put(payload)
            data.next = now + ttl * 0.75       
            self.statsd.incr('keepalive_emitted,tier=kontrol')

        if self.terminate:
            raise Aborted('resetting')

        return 'initial', data, 0.25

    def _shorten(self, n):

        #
        # - trivial base 62 encoder
        #
        out = ''
        alphabet = string.digits + string.lowercase + string.uppercase
        while n:
            out = alphabet[n % 62] + out
            n = int(n / 62)
        return out