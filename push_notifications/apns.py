"""
Apple Push Notification Service
Documentation is available on the iOS Developer Library:
https://developer.apple.com/library/ios/documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/Chapters/ApplePushService.html
"""

import logging
import time

from apns2 import client as apns2_client
from apns2 import errors as apns2_errors
from apns2 import payload as apns2_payload
from django.core.exceptions import ImproperlyConfigured
from gobiko.apns import APNsClient

from . import models
from . import NotificationError
from .apns_errors import reason_for_exception_class
from .settings import PUSH_NOTIFICATIONS_SETTINGS as SETTINGS

logger = logging.getLogger('push_notifications')


class APNSError(NotificationError):
    pass


class APNSUnsupportedPriority(APNSError):
    pass


class APNSServerError(APNSError):
    def __init__(self, status):
        super(APNSServerError, self).__init__(status)
        self.status = status


def _apns_create_socket(certfile=None):
    certfile = certfile or SETTINGS.get("APNS_CERTIFICATE")
    client = apns2_client.APNsClient(
        certfile,
        use_sandbox=SETTINGS.get("APNS_USE_SANDBOX"),
        use_alternative_port=SETTINGS.get("APNS_USE_ALTERNATIVE_PORT"))
    client.connect()
    return client


def _apns_prepare(
    token, alert, badge=None, sound=None, category=None, content_available=False,
    action_loc_key=None, loc_key=None, loc_args=[], extra={}, mutable_content=False,
    thread_id=None, url_args=None):
        if action_loc_key or loc_key or loc_args:
            apns2_alert = apns2_payload.PayloadAlert(
                body=alert if alert else {}, body_localized_key=loc_key,
                body_localized_args=loc_args, action_localized_key=action_loc_key)
        else:
            apns2_alert = alert

            if callable(badge):
                badge = badge(token)

        return apns2_payload.Payload(
            apns2_alert, badge, sound, content_available, mutable_content, category,
            url_args, custom=extra, thread_id=thread_id)


def _check_auth_key_settings():
    TEAM_ID = SETTINGS.get('TEAM_ID')
    BUNDLE_ID = SETTINGS.get('BUNDLE_ID')
    APNS_KEY_ID = SETTINGS.get('APNS_KEY_ID')
    APNS_KEY_FILEPATH = SETTINGS.get('APNS_KEY_FILEPATH')
    if not TEAM_ID:
        raise ImproperlyConfigured(
            'You need to set PUSH_NOTIFICATION_SETTINGS["TEAM_ID"]'
        )
    if not BUNDLE_ID:
        raise ImproperlyConfigured(
            'You need to set PUSH_NOTIFICATION_SETTINGS["BUNDLE_ID"]'
        )
    if not APNS_KEY_ID:
        raise ImproperlyConfigured(
            'You need to set PUSH_NOTIFICATION_SETTINGS["APNS_KEY_ID"]'
        )
    if not APNS_KEY_FILEPATH:
        raise ImproperlyConfigured(
            'You need to set PUSH_NOTIFICATION_SETTINGS["APNS_KEY_FILEPATH"]'
        )


def _auth_key_apns_send(registration_id, alert, **kwargs):
    TEAM_ID = SETTINGS.get('TEAM_ID')
    BUNDLE_ID = SETTINGS.get('BUNDLE_ID')
    APNS_KEY_ID = SETTINGS.get('APNS_KEY_ID')
    APNS_KEY_FILEPATH = SETTINGS.get('APNS_KEY_FILEPATH')
    APNS_USE_SANDBOX = SETTINGS.get('APNS_USE_SANDBOX', True)
    client = APNsClient(
        team_id=TEAM_ID,
        bundle_id=BUNDLE_ID,
        auth_key_id=APNS_KEY_ID,
        auth_key_filepath=APNS_KEY_FILEPATH,
        use_sandbox=APNS_USE_SANDBOX,
        force_proto='h2'
    )

    kwargs['alert'] = alert
    ret = client.send_message(registration_id, **kwargs)
    if ret == True:
        return 'True'
    else:
        return ret


def _auth_key_apns_bulk_send(registration_ids, alert, **kwargs):
    TEAM_ID = SETTINGS.get('TEAM_ID')
    BUNDLE_ID = SETTINGS.get('BUNDLE_ID')
    APNS_KEY_ID = SETTINGS.get('APNS_KEY_ID')
    APNS_KEY_FILEPATH = SETTINGS.get('APNS_KEY_FILEPATH')
    APNS_USE_SANDBOX = SETTINGS.get('APNS_USE_SANDBOX', True)

    client = APNsClient(
        team_id=TEAM_ID,
        bundle_id=BUNDLE_ID,
        auth_key_id=APNS_KEY_ID,
        auth_key_filepath=APNS_KEY_FILEPATH,
        use_sandbox=APNS_USE_SANDBOX,
        force_proto='h2'
    )

    kwargs['alert'] = alert
    return client.send_bulk_message(registration_ids, **kwargs)


def _apns_send(registration_id, alert, batch=False, **kwargs):
    client = _apns_create_socket(kwargs.pop("certfile", None))

    notification_kwargs = {}

    # if expiration isn"t specified use 1 month from now
    notification_kwargs["expiration"] = kwargs.pop("expiration", None)
    if not notification_kwargs["expiration"]:
        notification_kwargs["expiration"] = int(time.time()) + 2592000

    priority = kwargs.pop("priority", None)
    if priority:
        try:
            notification_kwargs["priority"] = apns2_client.NotificationPriority(str(priority))
        except ValueError:
            raise APNSUnsupportedPriority("Unsupported priority %d" % (priority))

    if batch:
        data = [apns2_client.Notification(
            token=rid, payload=_apns_prepare(rid, alert, **kwargs)) for rid in registration_id]
        return client.send_notification_batch(
            data, SETTINGS.get("APNS_TOPIC"), **notification_kwargs)

    data = _apns_prepare(registration_id, alert, **kwargs)
    client.send_notification(
        registration_id, data, SETTINGS.get("APNS_TOPIC"), **notification_kwargs)


def apns_send_message(registration_id, alert, **kwargs):
    """
    Sends an APNS notification to a single registration_id.
    This will send the notification as form data.
    If sending multiple notifications, it is more efficient to use
    apns_send_bulk_message()

    Note that if set alert should always be a string. If it is not set,
    it won"t be included in the notification. You will need to pass None
    to this for silent notifications.
    """

    if SETTINGS.get('USE_APNS_KEY', False):
        _check_auth_key_settings()
        print "sending using APNS key"
        return _auth_key_apns_send(registration_id, alert, **kwargs)
    else:
        try:
            _apns_send(registration_id, alert, **kwargs)
        except apns2_errors.APNsException as apns2_exception:
            if isinstance(apns2_exception, apns2_errors.Unregistered):
                device = models.APNSDevice.objects.get(registration_id=registration_id)
                device.active = False
                device.save()
            raise APNSServerError(status=reason_for_exception_class(apns2_exception.__class__))


def apns_send_bulk_message(registration_ids, alert, **kwargs):
    """
    Sends an APNS notification to one or more registration_ids.
    The registration_ids argument needs to be a list.

    Note that if set alert should always be a string. If it is not set,
    it won"t be included in the notification. You will need to pass None
    to this for silent notifications.
    """
    if SETTINGS.get('USE_APNS_KEY', False):
        _check_auth_key_settings()
        print "sending using APNS key"
        return _auth_key_apns_bulk_send(registration_ids, alert, **kwargs)
    else:
        results = _apns_send(registration_ids, alert, batch=True, **kwargs)
        inactive_tokens = [token for token, result in results.items() if result == "Unregistered"]
        models.APNSDevice.objects.filter(registration_id__in=inactive_tokens).update(active=False)
        return results
