# coding: utf-8
# Copyright 2012 Christian Hergert
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pymongo
from   pymongo                   import errors
from   pymongo.uri_parser        import parse_uri
from   twisted.internet          import defer, reactor, task
from   twisted.internet.protocol import ReconnectingClientFactory
from   txmongo.database          import Database
from   txmongo.protocol          import MongoProtocol, Query
from   twisted.python            import log
from   twisted.python.failure    import Failure

class _Connection(ReconnectingClientFactory):
    __notify_ready = None
    __discovered = None
    __index = -1
    __uri = None
    __conf_loop = None
    __conf_loop_seconds = 300.0
    # set this to True to enable connecting to discovered members of the
    # replica set (upon failure of connection to main uri)
    # NOTE currently the next-member selection is not very smart.
    # it would make sense to use the data provided by mongo and sort by
    # latency, for example.
    __use_discovered = False
    instance = None
    protocol = MongoProtocol
    maxDelay = 60

    def __init__(self, pool, uri):
        self.__discovered = []
        self.__notify_ready = []
        self.__pool = pool
        self.__uri = uri
        self.__conf_loop = task.LoopingCall(lambda: self.configure(self.instance))
        self.__conf_loop.start(self.__conf_loop_seconds, now=False)
        self.__reconnected = False
        self.__slave_ok = self.__uri['options'].get('slaveok', False)
        self.connected = False

    def buildProtocol(self, addr):
        # Build the protocol.
        p = ReconnectingClientFactory.buildProtocol(self, addr)

        log.err('connected to mongo %s' % addr)
        # on connection ensure we reset potential delays from previous
        # reconnect attempts
        self.resetDelay()

        # If we do not care about connecting to a slave, then we can simply
        # return the protocol now and fire that we are ready.
        if self.__slave_ok:
            p.connectionReady().addCallback(lambda _: self.setInstance(instance=p))
            # if we had reconnected, authenticate the db objects again
            if self.__reconnected:
                p.connectionReady().addCallback(lambda _: self.onReconnect())
            return p

        # Update our server configuration. This may disconnect if the node
        # is not a master.
        p.connectionReady().addCallback(lambda _: self.configure(p))

        return p

    @defer.inlineCallbacks
    def onReconnect(self):
        self.__reconnected = False
        for n, db in self.__pool._db_cache.iteritems():
            log.err('reauthenticating for db %s' % db)
            yield db.reauthenticate()

    def configure(self, proto):
        """
        Configures the protocol using the information gathered from the
        remote Mongo instance. Such information may contain the max
        BSON document size, replica set configuration, and the master
        status of the instance.
        """
        if proto:
            query = Query(collection='admin.$cmd', query={'ismaster': 1})
            df = proto.send_QUERY(query)
            df.addCallback(self._configureCallback, proto)
            return df
        return defer.succeed(None)

    def _configureCallback(self, reply, proto):
        """
        Handle the reply from the "ismaster" query. The reply contains
        configuration information about the peer.
        """
        # Make sure we got a result document.
        if len(reply.documents) != 1:
            proto.fail(errors.OperationFailure('Invalid document length.'))
            return

        # Get the configuration document from the reply.
        config = reply.documents[0].decode()

        # Make sure the command was successful.
        if not config.get('ok'):
            code = config.get('code')
            msg = config.get('err', 'Unknown error')
            proto.fail(errors.OperationFailure(msg, code))
            return

        # Check that the replicaSet matches.
        set_name = config.get('setName')
        expected_set_name = self.uri['options'].get('setname')
        if expected_set_name and (expected_set_name != set_name):
            # Log the invalid replica set failure.
            msg = 'Mongo instance does not match requested replicaSet.'
            reason = pymongo.errors.ConfigurationError(msg)
            proto.fail(reason)
            return

        # Track max bson object size limit.
        max_bson_size = config.get('maxBsonObjectSize')
        if max_bson_size:
            proto.max_bson_size = max_bson_size

        # Track the other hosts in the replica set.
        hosts = config.get('hosts')
        if isinstance(hosts, list) and hosts:
            hostaddrs = []
            for host in hosts:
                if ':' not in host:
                    host = (host, 27017)
                else:
                    host = host.split(':', 1)
                    host[1] = int(host[1])
                hostaddrs.append(host)
            self.__discovered = hostaddrs

        # Check if this node is the master.
        ismaster = config.get('ismaster')
        if not self.__slave_ok and not ismaster:
            reason = pymongo.errors.AutoReconnect('not master')
            proto.fail(reason)
            return

        # Notify deferreds waiting for completion.
        self.setInstance(instance=proto)
        # if we had reconnected, authenticate the db objects again
        if self.__reconnected:
            proto.connectionReady().addCallback(lambda _: self.onReconnect())

    def clientConnectionFailed(self, connector, reason):
        log.err('mongo connection failed: %s' % reason.getErrorMessage())
        self.connected = False
        if self.continueTrying:
            self.connector = connector
            self.retryNextHost()

    def clientConnectionLost(self, connector, reason):
        log.err('mongo connection lost: %s' % reason.getErrorMessage())
        self.connected = False
        if self.continueTrying:
            self.connector = connector
            self.retryNextHost()

    def notifyReady(self):
        """
        Returns a deferred that will fire when the factory has created a
        protocol that can be used to communicate with a Mongo server.

        Note that this will not fire until we have connected to a Mongo
        master, unless slaveOk was specified in the Mongo URI connection
        options.
        """
        if self.instance:
            return defer.succeed(self.instance)
        if self.__notify_ready is None:
            self.__notify_ready = []
        df = defer.Deferred()
        self.__notify_ready.append(df)
        return df

    def retryNextHost(self, connector=None):
        """
        Have this connector connect again, to the next host in the
        configured list of hosts.
        """
        if not self.continueTrying:
            log.err("Abandoning %s on explicit request" % (connector,))
            return

        if connector is None:
            if self.connector is None:
                raise ValueError("no connector to retry")
            else:
                connector = self.connector

        delay = False
        self.__index += 1

        allNodes = list(self.uri['nodelist'])
        if self.__use_discovered:
            allNodes += list(self.__discovered)
        if self.__index >= len(allNodes):
            self.__index = 0
            delay = True

        connector.host, connector.port = allNodes[self.__index]

        log.err('attempting mongo reconnect to %s:%s' %
                  (connector.host, connector.port))

        self.__reconnected = True
        if delay:
            self.retry(connector)
        else:
            connector.connect()

    def setInstance(self, instance=None, reason=None):
        if self.instance and self.instance != instance:
            self.instance.connectionLost(Failure(Exception('reconnection')))
        if instance:
            self.connected = True
        self.instance = instance
        deferreds, self.__notify_ready = self.__notify_ready, []
        if deferreds:
            for df in deferreds:
                if instance:
                    df.callback(self)
                else:
                    df.errback(reason)

    def stopTrying(self):
        ReconnectingClientFactory.stopTrying(self)
        self.__conf_loop.stop()

    @property
    def uri(self):
        return self.__uri

class ConnectionPool(object):
    __index = 0
    __pool = None
    __pool_size = None
    __uri = None

    def __init__(self, uri='mongodb://127.0.0.1:27017', pool_size=1):
        assert isinstance(uri, basestring)
        assert isinstance(pool_size, int)
        assert pool_size >= 1

        if not uri.startswith('mongodb://'):
            uri = 'mongodb://' + uri

        self.__uri = parse_uri(uri)
        self.__pool_size = pool_size
        self.__pool = [_Connection(self, self.__uri) for i in xrange(pool_size)]

        self._db_cache = {}

        host, port = self.__uri['nodelist'][0]
        for factory in self.__pool:
            factory.connector = reactor.connectTCP(host, port, factory)

    def getprotocols(self):
        return self.__pool

    def __getitem__(self, name):
        if name in self._db_cache:
            return self._db_cache[name]
        db = Database(self, name)
        self._db_cache[name] = db
        return db

    def __getattr__(self, name):
        return self[name]

    def __repr__(self):
        if self.uri['nodelist']:
            return 'Connection(%r, %r)' % self.uri['nodelist'][0]
        return 'Connection()'

    def disconnect(self):
        for factory in self.__pool:
            factory.stopTrying()
            factory.stopFactory()
            if factory.instance and factory.instance.transport:
                factory.instance.transport.loseConnection()
            if factory.connector:
                factory.connector.disconnect()
        # Wait for the next iteration of the loop for resolvers
        # to potentially cleanup.
        df = defer.Deferred()
        reactor.callLater(0, df.callback, None)
        return df

    def isconnected(self):
        connection = self.__pool[self.__index]
        return connection.connected

    def getprotocol(self):
        # Get the next protocol available for communication in the pool.
        connection = self.__pool[self.__index]
        self.__index = (self.__index + 1) % self.__pool_size

        # If the connection is already connected, just return it.
        if connection.instance:
            return defer.succeed(connection.instance)

        # Wait for the connection to connection.
        return connection.notifyReady().addCallback(lambda c: c.instance)

    @property
    def uri(self):
        return self.__uri

Connection = ConnectionPool

###
# Begin Legacy Wrapper
###

class MongoConnection(Connection):
    def __init__(self, host, port, pool_size=1):
        uri = 'mongodb://%s:%d/' % (host, port)
        Connection.__init__(self, uri, pool_size=pool_size)
lazyMongoConnectionPool = MongoConnection
lazyMongoConnection = MongoConnection
MongoConnectionPool = MongoConnection

###
# End Legacy Wrapper
###

if __name__ == '__main__':
    import sys

    log.startLogging(sys.stdout)
    connection = Connection()
    reactor.run()
