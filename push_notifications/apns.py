"""
Apple Push Notification Service
Documentation is available on the iOS Developer Library:
https://developer.apple.com/library/ios/#documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/ApplePushService/ApplePushService.html
"""

import json
import struct
import urllib2
import ssl
import socket
import time
from contextlib import closing
from binascii import unhexlify
from django.conf import settings
from . import NotificationError
from .settings import PUSH_NOTIFICATIONS_SETTINGS as SETTINGS
from .exceptions import NotificationPayloadSizeExceeded, InvalidPassPhrase


class APNSError(NotificationError):
    pass


class APNSServerError(APNSError):
    def __init__(self, status, identifier):
        super(APNSServerError, self).__init__(status, identifier)
        self.status = status
        self.identifier = identifier


class APNSDataOverflow(APNSError):
    pass


APNS_MAX_NOTIFICATION_SIZE = 256

SETTINGS.setdefault("APNS_PORT", 2195)
if settings.DEBUG:
    SETTINGS.setdefault("APNS_HOST", "gateway.sandbox.push.apple.com")
else:
    SETTINGS.setdefault("APNS_HOST", "gateway.push.apple.com")


def _apns_create_socket(address_tuple):
    """
    Establishes an encrypted SSL socket connection to the service.
    After connecting the socket can be written to or read from.
    """
    import ssl
    import socket
    import OpenSSL
    from django.core.exceptions import ImproperlyConfigured

    # ssl in Python < 3.2 does not support certificates/keys as strings.
    # See http://bugs.python.org/issue3823
    # Therefore pyOpenSSL which lets us do this is a dependancy.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    certificate = SETTINGS.get("APNS_CERTIFICATE")
    private_key = SETTINGS.get("APNS_PRIVATE_KEY")
    passphrase = SETTINGS.get("APNS_PRIVATE_KEY_PASSPHRASE")
    cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, certificate)
    args = [OpenSSL.crypto.FILETYPE_PEM, private_key]
    if passphrase is not None:
        args.append(str(passphrase))
    try:
        pkey = OpenSSL.crypto.load_privatekey(*args)
    except OpenSSL.crypto.Error:
        raise InvalidPassPhrase
    context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv3_METHOD)
    context.use_certificate(cert)
    context.use_privatekey(pkey)
    connection = OpenSSL.SSL.Connection(context, sock)
    connection.connect(address_tuple)
    connection.set_connect_state()
    connection.do_handshake()
    return connection


def _apns_create_socket_old():
    import ssl
    from socket import socket
    from django.core.exceptions import ImproperlyConfigured

    sock = socket()
    certfile = SETTINGS.get("APNS_CERTIFICATE")
    if not certfile:
        raise ImproperlyConfigured(
            'You need to set PUSH_NOTIFICATIONS_SETTINGS["APNS_CERTIFICATE"] to send messages through APNS.')

    try:
        f = open(certfile, "r")
        f.read()
        f.close()
    except Exception, e:
        raise ImproperlyConfigured("The APNS certificate file at %r is not readable: %s" % (certfile, e))

    sock = ssl.wrap_socket(sock, ssl_version=ssl.PROTOCOL_SSLv3, certfile=certfile)
    sock.connect((SETTINGS["APNS_HOST"], SETTINGS["APNS_PORT"]))

    return sock


def _apns_create_socket_to_push():
    return _apns_create_socket((SETTINGS["APNS_HOST"], SETTINGS["APNS_PORT"]))


def _apns_create_socket_to_feedback():
    return _apns_create_socket((SETTINGS["APNS_FEEDBACK_HOST"], SETTINGS["APNS_FEEDBACK_PORT"]))


def _apns_pack_frame(token_hex, payload, identifier, expiration, priority):
    token = unhexlify(token_hex)
    # |COMMAND|FRAME-LEN|{token}|{payload}|{id:4}|{expiration:4}|{priority:1}
    frame_len = 3 * 5 + len(token) + len(payload) + 4 + 4 + 1  # 5 items, each 3 bytes prefix, then each item length
    frame_fmt = "!BIBH%ssBH%ssBHIBHIBHB" % (len(token), len(payload))
    frame = struct.pack(
        frame_fmt,
        2, frame_len,
        1, len(token), token,
        2, len(payload), payload,
        3, 4, identifier,
        4, 4, expiration,
        5, 1, priority)

    return frame


def _apns_check_errors(sock):
    timeout = SETTINGS["APNS_ERROR_TIMEOUT"]
    if timeout is None:
        return  # assume everything went fine!
    saved_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        data = sock.recv(6)
        if data:
            command, status, identifier = struct.unpack("!BBI", data)
            # apple protocol says command is always 8. See http://goo.gl/ENUjXg
            assert command == 8, "Command must be 8!"
            if status != 0:
                raise APNSServerError(status, identifier)
    except socket.timeout:  # py3
        pass
    except ssl.SSLError as e:  # py2
        if "timed out" not in e.message:
            raise
    finally:
        sock.settimeout(saved_timeout)


def _apns_send(token, alert, badge=None, sound=None, category=None, content_available=False,
               action_loc_key=None, loc_key=None, loc_args=[], extra={}, identifier=0,
               expiration=None, priority=10, socket=None):
    data = {}
    aps_data = {}

    if action_loc_key or loc_key or loc_args:
        alert = {"body": alert} if alert else {}
        if action_loc_key:
            alert["action-loc-key"] = action_loc_key
        if loc_key:
            alert["loc-key"] = loc_key
        if loc_args:
            alert["loc-args"] = loc_args

    if alert is not None:
        aps_data["alert"] = alert

    if badge is not None:
        aps_data["badge"] = badge

    if sound is not None:
        aps_data["sound"] = sound

    if category is not None:
        aps_data["category"] = category

    if content_available:
        aps_data["content-available"] = 1

    data["aps"] = aps_data
    data.update(extra)

    # convert to json, avoiding unnecessary whitespace with separators
    json_data = json.dumps(data, separators=(",", ":")).encode("utf-8")

    if len(json_data) > APNS_MAX_NOTIFICATION_SIZE:
        raise APNSDataOverflow("Notification body cannot exceed %i bytes" % (APNS_MAX_NOTIFICATION_SIZE))

    # if expiration isn't specified use 1 month from now
    expiration_time = expiration if expiration is not None else int(time.time()) + 2592000

    frame = _apns_pack_frame(token, json_data, identifier, expiration_time, priority)

    if socket:
        socket.write(frame)
    else:
        with closing(_apns_create_socket_to_push()) as socket:
            socket.write(frame)
            _apns_check_errors(socket)


def _apns_read_and_unpack(socket, data_format):
    length = struct.calcsize(data_format)
    data = socket.recv(length)
    if data:
        return struct.unpack_from(data_format, data, 0)
    else:
        return None


def _apns_receive_feedback(socket):
    expired_token_list = []

    # read a timestamp (4 bytes) and device token length (2 bytes)
    header_format = '!LH'
    has_data = True
    while has_data:
        try:
            # read the header tuple
            header_data = _apns_read_and_unpack(socket, header_format)
            if header_data is not None:
                timestamp, token_length = header_data
                # Unpack format for a single value of length bytes
                token_format = '%ss' % token_length
                device_token = _apns_read_and_unpack(socket, token_format)
                if device_token is not None:
                    # _apns_read_and_unpack returns a tuple, but
                    # it's just one item, so get the first.
                    expired_token_list.append((timestamp, device_token[0]))
            else:
                has_data = False
        except ssl.SSLError as e:  # py2
            if "timed out" not in e.message:
                raise

    return expired_token_list


def apns_send_message(registration_id, alert, **kwargs):
    """
    Sends an APNS notification to a single registration_id.
    This will send the notification as form data.
    If sending multiple notifications, it is more efficient to use
    apns_send_bulk_message()

    Note that if set alert should always be a string. If it is not set,
    it won't be included in the notification. You will need to pass None
    to this for silent notifications.
    """

    return _apns_send(registration_id, alert, **kwargs)


def apns_send_bulk_message(registration_ids, alert, **kwargs):
    """
    Sends an APNS notification to one or more registration_ids.
    The registration_ids argument needs to be a list.

    Note that if set alert should always be a string. If it is not set,
    it won't be included in the notification. You will need to pass None
    to this for silent notifications.
    """
    with closing(_apns_create_socket_to_push()) as socket:
        for identifier, registration_id in enumerate(registration_ids):
            _apns_send(registration_id, alert, identifier=identifier, socket=socket, **kwargs)
        _apns_check_errors(socket)


def apns_fetch_inactive_ids():
    """
    Queries the APNS server for id's that are no longer active since
    the last fetch
    """
    with closing(_apns_create_socket_to_feedback()) as socket:
        inactive_ids = []
        # Maybe we should have a flag to return the timestamp?
        # It doesn't seem that useful right now, though.
        for tStamp, registration_id in _apns_receive_feedback(socket):
            inactive_ids.append(registration_id.encode('hex'))
        return inactive_ids
