# -*- coding: utf-8 -*-

import pika
import json
import time
import logging
import threading

from . import logs


class RMQueue(object):
    """This publisher will handle failures and closures and will
    attempt to restart things

    delivery confirmations are used to track messages that have
    been sent and if they have been confirmed

    """

    def __init__(self, queue, route, amqp_url='',
                 config=None, logger=None, verbose=None):
        """Setup the example publisher object, passing in the URL we will use
        to connect to RabbitMQ.
        """
        self._rmq_lock = threading.RLock()
        self._connection = None
        self._channel = None
        self._consumer = None
        self._consume_ready = False

        self._msg_tracking_lock = threading.RLock()
        self._deliveries = []
        self._acked = 0
        self._nacked = 0
        self._message_number = 0

        self._rmq_msg = None
        self._rmq_id = None

        self._stopping = False
        self._exchange = 'StudioML.topic'
        self._exchange_type = 'topic'
        self._routing_key = route

        self._url = amqp_url

        if logger is not None:
            self._logger = logger
        else:
            self._logger = logs.getLogger('RabbitMQ')
            self._logger.setLevel(logging.INFO)

        if config is not None:
            # extract from the config data structure any settings related to
            # queue messaging for rabbit MQ
            if 'cloud' in config:
                if 'queue' in config['cloud']:
                    if 'rmq' in config['cloud']['queue']:
                        self._url = config['cloud']['queue']['rmq']
                        self._logger.warn('url {}'
                                          .format(self._url))

        self._queue = queue

        # The pika library for RabbitMQ has an asynchronous run method
        # that needs to run forever and will do reconnections etc
        # automatically for us
        thr = threading.Thread(target=self.run, args=(), kwargs={})
        thr.setDaemon(True)
        thr.start()

    def connect(self):
        """
        When the connection is established, the on_connection_open method
        will be invoked by pika. If you want the reconnection to work, make
        sure you set stop_ioloop_on_close to False, which is not the default
        behavior of this adapter.

        :rtype: pika.SelectConnection

        """
        params = pika.URLParameters(self._url)
        return pika.SelectConnection(
            params,
            on_open_callback=self.on_connection_open,
            on_close_callback=self.on_connection_closed,
            stop_ioloop_on_close=False)

    def on_connection_open(self, unused_connection):
        """
        :type unused_connection: pika.SelectConnection
        """
        self.open_channel()

    def on_connection_closed(self, connection, reply_code, reply_text):
        """
        on any close reconnect to RabbitMQ, until the stopping is set

        :param pika.connection.Connection connection: The closed connection obj
        :param int reply_code: The server provided reply_code if given
        :param str reply_text: The server provided reply_text if given

        """
        with self._rmq_lock:
            self._channel = None
            if self._stopping:
                self._connection.ioloop.stop()
            else:
                # retry in 5 seconds
                self._logger.info('connection closed, retry in 5 seconds: ' +
                                  str(reply_code) + ' ' + reply_text)
                self._connection.add_timeout(5, self._connection.ioloop.stop)

    def open_channel(self):
        """
        open a new channel using the Channel.Open RPC command. RMQ confirms
        the channel is open by sending the Channel.OpenOK RPC reply, the
        on_channel_open method will be invoked.
        """
        self._logger.debug('creating a new channel')

        with self._rmq_lock:
            self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        """
        on channel open, declare the exchange to use

        :param pika.channel.Channel channel: The channel object

        """
        self._logger.debug('created a new channel')

        with self._rmq_lock:
            self._channel = channel
            self._channel.basic_qos(prefetch_count=0)
            self._channel.add_on_close_callback(self.on_channel_closed)

        self.setup_exchange(self._exchange)

    def on_channel_closed(self, channel, reply_code, reply_text):
        """
        physical network issues and logical protocol abuses can
        result in a closure of the channel.

        :param pika.channel.Channel channel: The closed channel
        :param int reply_code: The numeric reason the channel was closed
        :param str reply_text: The text reason the channel was closed

        """
        self._logger.info(
            'channel closed ' +
            str(reply_code) +
            ' ' +
            reply_text)
        with self._rmq_lock:
            self._channel = None
            if not self._stopping:
                self._connection.close()

    def setup_exchange(self, exchange_name):
        """
        exchange setup by invoking the Exchange.Declare RPC command.
        When complete, the on_exchange_declareok method will be invoked
        by pika.

        :param str|unicode exchange_name: The name of the exchange to declare

        """
        self._logger.debug('declaring exchange ' + exchange_name)
        with self._rmq_lock:
            self._channel.exchange_declare(callback=self.on_exchange_declareok,
                                           exchange=exchange_name,
                                           exchange_type=self._exchange_type,
                                           durable=True,
                                           auto_delete=True)

    def on_exchange_declareok(self, unused_frame):
        """
        completion callback for the Exchange.Declare RPC command.

        :param pika.Frame.Method unused_frame: Exchange.DeclareOk response

        """
        self._logger.debug('declared exchange ' + self._exchange)
        self.setup_queue(self._queue)

    def setup_queue(self, queue_name):
        """
        Setup the queue invoking the Queue.Declare RPC command.
        The completion callback is, the on_queue_declareok method.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        self._logger.debug('declare queue ' + queue_name)
        with self._rmq_lock:
            self._channel.queue_declare(self.on_queue_declareok, queue_name)

    def on_queue_declareok(self, method_frame):
        """
        Queue.Declare RPC completion callback.
        In this method the queue and exchange are bound together
        with the routing key by issuing the Queue.Bind
        RPC command.

        The completion callback is the on_bindok method.

        :param pika.frame.Method method_frame: The Queue.DeclareOk frame

        """
        self._logger.debug(
            'binding ' +
            self._exchange +
            ' to ' +
            self._queue +
            ' with ' +
            self._routing_key)
        with self._rmq_lock:
            self._channel.queue_bind(self.on_bindok, self._queue,
                                     self._exchange, self._routing_key)

    def on_bindok(self, unused_frame):
        """This method is invoked by pika when it receives the Queue.BindOk
        response from RabbitMQ. Since we know we're now setup and bound, it's
        time to start publishing."""
        self._logger.info(
            'bound ' +
            self._exchange +
            ' to ' +
            self._queue +
            ' with ' +
            self._routing_key)

        """
        Send the Confirm.Select RPC method to RMQ to enable delivery
        confirmations on the channel. The only way to turn this off is to close
        the channel and create a new one.

        When the message is confirmed from RMQ, the
        on_delivery_confirmation method will be invoked passing in a Basic.Ack
        or Basic.Nack method from RMQ that will indicate which messages it
        is confirming or rejecting.
        """
        with self._rmq_lock:
            self._channel.confirm_delivery(self.on_delivery_confirmation)

    def on_delivery_confirmation(self, method_frame):
        """
        RMQ callback for responses to a Basic.Publish RPC
        command, passing in either a Basic.Ack or Basic.Nack frame with
        the delivery tag of the message that was published. The delivery tag
        is an integer counter indicating the message number that was sent
        on the channel via Basic.Publish. Here we're just doing house keeping
        to keep track of stats and remove message numbers that we expect
        a delivery confirmation of from the list used to keep track of messages
        that are pending confirmation.

        :param pika.frame.Method method_frame: Basic.Ack or Basic.Nack frame

        """
        confirmation_type = method_frame.method.NAME.split('.')[1].lower()
        self._logger.debug('received ' +
                           confirmation_type +
                           ' for delivery tag: ' +
                           str(method_frame.method.delivery_tag))

        with self._msg_tracking_lock:
            if confirmation_type == 'ack':
                self._acked += 1
            elif confirmation_type == 'nack':
                self._nacked += 1
            self._deliveries.remove(method_frame.method.delivery_tag)
            self._logger.info('published ' +
                              str(self._message_number) +
                              ' messages, ' +
                              str(len(self._deliveries)) +
                              ' have yet to be confirmed, ' +
                              str(self._acked) +
                              ' were acked and ' +
                              str(self._nacked) +
                              ' were nacked')

    def run(self):
        """
        Blocking run loop, connecting and then starting the IOLoop.
        """
        self._logger.info('RMQ started')
        while not self._stopping:
            self._connection = None
            with self._msg_tracking_lock:
                self._deliveries = []
                self._acked = 0
                self._nacked = 0
                self._message_number = 0

            try:
                with self._rmq_lock:
                    self._connection = self.connect()
                self._logger.info('RMQ connected')
                self._connection.ioloop.start()
            except KeyboardInterrupt:
                self.stop()
                if (self._connection is not None and
                        not self._connection.is_closed):
                    # Finish closing
                    self._connection.ioloop.start()

        self._logger.info('RMQ stopped')

    def stop(self):
        """
        Stop the by closing the channel and connection and setting
        a stop state.

        The IOLoop is started independently which means we need this
        method to handle things such as the Try/Catch when KeyboardInterrupts
        are caught.
        Starting the IOLoop again will allow the publisher to cleanly
        disconnect from RMQ.
        """
        self._logger.info('stopping')
        self._stopping = True
        self.close_channel()
        self.close_connection()

    def close_channel(self):
        """
        Close channel by sending the Channel.Close RPC command.
        """
        with self._rmq_lock:
            if self._channel is not None:
                self._logger.info('closing the channel')
                self._channel.close()

    def close_connection(self):
        with self._rmq_lock:
            if self._connection is not None:
                self._logger.info('closing connection')
                self._connection.close()

    def clean(self, timeout=0):
        while True:
            msg = self.dequeue(timeout=timeout)
            if not msg:
                break
        return

    def get_name(self):
        return self._queue

    def enqueue(self, msg, retries=10):
        """
        Publish a message to RMQ, appending a list of deliveries with
        the message number that was sent.  This list will be used to
        check for delivery confirmations in the
        on_delivery_confirmations method.
        """
        if self._url is None:
            raise Exception('url for rmq not initialized')

        if msg is None:
            raise Exception(
                'message was None, it needs a meaningful value to be sent')

        # Wait to see if the channel gets opened
        tries = retries
        while tries != 0:
            if self._channel is None:
                self._logger.warn(
                    'failed to send message ({} tries left) to {} as '
                    'the channel API was not initialized' .format(
                        tries, self._url))
            elif not self._channel.is_open:
                self._logger.warn(
                    'failed to send message ({} tries left) to {} as '
                    'the channel was not open' .format(
                        tries, self._url))
            else:
                break

            time.sleep(1)
            tries -= 1

        if tries == 0:
            raise Exception('studioml request could not be sent')

        self._logger.debug('sending message {} to {} '
                           .format(msg, self._url))
        properties = pika.BasicProperties(app_id='studioml',
                                          content_type='application/json')

        self._channel.basic_publish(exchange=self._exchange,
                                    routing_key=self._routing_key,
                                    body=msg,
                                    properties=properties,
                                    mandatory=True)
        self._logger.debug('sent message to {} '
                           .format(self._url))

        message_number = 0
        with self._msg_tracking_lock:
            self._message_number += 1

            message_number = self._message_number
            self._deliveries.append(self._message_number)

        tries = retries
        while tries != 0:
            time.sleep(1)

            with self._msg_tracking_lock:
                if message_number not in self._deliveries:
                    self._logger.debug('sent message acknowledged to {} ' +
                                       'after waiting {} seconds'
                                       .format(self._url, abs(tries - 5)))

                    return message_number
                else:
                    tries -= 1

        raise Exception('studioml message was never acknowledged to {} ' +
                        'after waiting {} seconds'
                        .format(self._url, abs(tries - 5)))

    def dequeue(self, acknowledge=True, timeout=0):
        msg = None

        # start the consumer and allow single messages to returned via
        # this method to the caller blocking using a callback lock
        # while waiting
        for i in range(timeout + 1):
            with self._rmq_lock:
                if self._consumer is None and self._channel is not None:
                    self._consumer = \
                        self._channel.basic_consume(self.on_message,
                                                    queue=self._queue)

                if self._rmq_msg is not None:
                    self._logger.info('message {} from {} '
                                      .format(self._rmq_msg, self._url))
                    return self._rmq_msg, self._rmq_id
                else:
                    self._logger.info('idle {} {}'
                                      .format(self._url, self._queue))

            if i >= timeout:
                self._logger.info('timed-out')
                return None

            time.sleep(1)

        self._logger.info('dequeue done')

    def on_message(self, unused_channel, basic_deliver, properties, body):

        with self._rmq_lock:
            if self._channel is not None:
                # Cancel the consumer as we only consume 1 message
                # at a time
                self._channel.basic_cancel(
                    nowait=True, consumer_tag=self._consumer)
            self._consumer = None

            # If we already had a delivered message, reject the one we just got
            if self._rmq_msg is not None:
                if self._connection is not None:
                    self._channel.basic_nack(
                        delivery_tag=basic_deliver.delivery_tag)
            else:
                self._rmq_msg = body
                self._rmq_id = basic_deliver.delivery_tag

    def has_next(self):
        raise NotImplementedError(
            'using has_next with distributed queue is not supportable')

    def acknowledge(self, ack_id):
        with self._rmq_lock:
            self._rmq_msg = None
            self._rmq_id = None
            if self._channel is None:
                return None
            result = self._channel.basic_ack(delivery_tag=ack_id)

    def hold(self, ack_id, minutes):
        # Nothing is needed here as the message will remain while the channel
        # remains open, or we nack it
        pass

    def delete(self):
        raise NotImplementedError('')
