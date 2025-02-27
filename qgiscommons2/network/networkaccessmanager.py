# -*- coding: utf-8 -*-
"""
***************************************************************************
    An httplib2 replacement that uses QgsNetworkAccessManager

    ---------------------
    Date                 : August 2016
    Copyright            : (C) 2016 Boundless, http://boundlessgeo.com
    Email                : apasotti at boundlessgeo dot com
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

from builtins import str
from builtins import object

__author__ = 'Alessandro Pasotti'
__date__ = 'August 2016'

import re
import io
import urllib.request, urllib.error, urllib.parse

from qgis.PyQt.QtCore import pyqtSlot, QUrl, QEventLoop, QTimer, QCoreApplication
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    QgsApplication,
    QgsNetworkAccessManager,
    QgsAuthManager,
    QgsMessageLog
)

DEFAULT_MAX_REDIRECTS = 4

class RequestsException(Exception):
    pass

class RequestsExceptionTimeout(RequestsException):
    pass

class RequestsExceptionConnectionError(RequestsException):
    pass

class RequestsExceptionUserAbort(RequestsException):
    pass

class RequestsMaxRedirects(RequestsException):
    pass

class Map(dict):
    """
    Example:
    m = Map({'first_name': 'Eduardo'}, last_name='Pool', age=24, sports=['Soccer'])
    """
    def __init__(self, *args, **kwargs):
        super(Map, self).__init__(*args, **kwargs)
        for arg in args:
            if isinstance(arg, dict):
                for k, v in arg.items():
                    self[k] = v

        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    def __getattr__(self, attr):
        return self.get(attr)

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __setitem__(self, key, value):
        super(Map, self).__setitem__(key, value)
        self.__dict__.update({key: value})

    def __delattr__(self, item):
        self.__delitem__(item)

    def __delitem__(self, key):
        super(Map, self).__delitem__(key)
        del self.__dict__[key]


class Response(Map):
    pass

class NetworkAccessManager(object):
    """
    This class mimicks httplib2 by using QgsNetworkAccessManager for all
    network calls.

    The return value is a tuple of (response, content), the first being and
    instance of the Response class, the second being a string that contains
    the response entity body.

    Parameters
    ----------
    debug : bool
        verbose logging if True
    exception_class : Exception
        Custom exception class

    Usage 1 (blocking mode)
    -----
    ::
        nam = NetworkAccessManager(authcgf)
        try:
            (response, content) = nam.request('http://www.example.com')
        except RequestsException as e:
            # Handle exception
            pass

    Usage 2 (Non blocking mode)
    -------------------------
    ::
        NOTE! if blocking mode returns immediatly
              it's up to the caller to manage listeners in case
              of non blocking mode

        nam = NetworkAccessManager(authcgf)
        try:
            nam.request('http://www.example.com', blocking=False)
            nam.reply.finished.connect(a_signal_listener)
        except RequestsException as e:
            # Handle exception
            pass

        Get response using method:
        nam.httpResult() that return a dictionary with keys:
            'status' - http code result come from reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            'status_code' - http code result come from reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            'status_message' - reply message string from reply.attribute(QNetworkRequest.HttpReasonPhraseAttribute)
            'content' - bytearray returned from reply
            'ok' - request success [True, False]
            'headers' - Dicionary containing the reply header
            'reason' - fomatted message string with reply.errorString()
            'exception' - the exception returne dduring execution
    """

    def __init__(self, authid=None, disable_ssl_certificate_validation=False, exception_class=None, debug=True):
        self.disable_ssl_certificate_validation = disable_ssl_certificate_validation
        self.authid = authid
        self.reply = None
        self.debug = debug
        self.exception_class = exception_class
        self.on_abort = False
        self.blocking_mode = False
        self.http_call_result = Response({
            'status': 0,
            'status_code': 0,
            'status_message': '',
            'content' : '',
            'ok': False,
            'headers': {},
            'reason': '',
            'exception': None,
        })

    def msg_log(self, msg):
        if self.debug:
            QgsMessageLog.logMessage(msg, "NetworkAccessManager")

    def httpResult(self):
        return self.http_call_result

    def auth_manager(self):
        return QgsApplication.authManager()

    def request(self, url, method="GET", body=None, headers=None, redirections=DEFAULT_MAX_REDIRECTS, connection_type=None, blocking=True):
        """
        Make a network request by calling QgsNetworkAccessManager.
        """
        self.calling_args = locals()
        del self.calling_args['self']
        
        self.msg_log(u'http_call request: {0}'.format(url))

        self.blocking_mode = blocking
        req = QNetworkRequest()
        # Avoid double quoting form QUrl
        url = urllib.parse.unquote(url)
        req.setUrl(QUrl(url))
        if headers is not None:
            # This fixes a wierd error with compressed content not being correctly
            # inflated.
            # If you set the header on the QNetworkRequest you are basically telling
            # QNetworkAccessManager "I know what I'm doing, please don't do any content
            # encoding processing".
            # See: https://bugs.webkit.org/show_bug.cgi?id=63696#c1
            try:
                del headers['Accept-Encoding']
            except KeyError:
                pass
            for k, v in list(headers.items()):
                self.msg_log("Setting header %s to %s" % (k, v))
                req.setRawHeader(k.encode(), v.encode())
        if self.authid:
            self.msg_log("Update request w/ authid: {0}".format(self.authid))
            self.auth_manager().updateNetworkRequest(req, self.authid)
        if self.reply is not None and self.reply.isRunning():
            self.reply.close()
        if method.lower() == 'delete':
            func = getattr(QgsNetworkAccessManager.instance(), 'deleteResource')
        else:
            func = getattr(QgsNetworkAccessManager.instance(), method.lower())
        # Calling the server ...
        # Let's log the whole call for debugging purposes:
        self.msg_log("Sending %s request to %s" % (method.upper(), req.url().toString()))
        self.on_abort = False
        headers = {str(h): str(req.rawHeader(h)) for h in req.rawHeaderList()}
        for k, v in list(headers.items()):
            self.msg_log("%s: %s" % (k, v))
        if method.lower() in ['post', 'put']:
            if isinstance(body, io.IOBase):
                body = body.read()
            if isinstance(body, str):
                body = body.encode()
            self.reply = func(req, body)
        else:
            self.reply = func(req)
        if self.authid:
            self.msg_log("Update reply w/ authid: {0}".format(self.authid))
            self.auth_manager().updateNetworkReply(self.reply, self.authid)

        # necessary to trap local timout manage by QgsNetworkAccessManager
        # calling QgsNetworkAccessManager::abortRequest
        QgsNetworkAccessManager.instance().requestTimedOut.connect(self.requestTimedOut)

        self.reply.sslErrors.connect(self.sslErrors)
        self.reply.finished.connect(self.replyFinished)
        self.reply.downloadProgress.connect(self.downloadProgress)

        # block if blocking mode otherwise return immediatly
        # it's up to the caller to manage listeners in case of no blocking mode
        if not self.blocking_mode:
            return (None, None)

        # Call and block
        self.el = QEventLoop()
        self.reply.finished.connect(self.el.quit)

        # Catch all exceptions (and clean up requests)
        try:
            self.el.exec_(QEventLoop.ExcludeUserInputEvents)
        except Exception as e:
            raise e

        if self.reply:
            self.reply.finished.disconnect(self.el.quit)

        # emit exception in case of error
        if not self.http_call_result.ok:
            if self.http_call_result.exception and not self.exception_class:
                raise self.http_call_result.exception
            else:
                raise self.exception_class(self.http_call_result.reason)

        return (self.http_call_result, self.http_call_result.content)

    def downloadProgress(self, bytesReceived, bytesTotal):
        """Keep track of the download progress"""
        #self.msg_log("downloadProgress %s of %s ..." % (bytesReceived, bytesTotal))
        pass

    def requestTimedOut(self, reply):
        """Trap the timeout. In Async mode requestTimedOut is called after replyFinished"""
        # adapt http_call_result basing on receiving qgs timer timout signal
        self.exception_class = RequestsExceptionTimeout
        self.http_call_result.exception = RequestsExceptionTimeout("Timeout error")

    def replyFinished(self):
        err = self.reply.error()
        httpStatus = self.reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        httpStatusMessage = self.reply.attribute(QNetworkRequest.HttpReasonPhraseAttribute)
        self.http_call_result.status_code = httpStatus
        self.http_call_result.status = httpStatus
        self.http_call_result.status_message = httpStatusMessage
        for k, v in self.reply.rawHeaderPairs():
            self.http_call_result.headers[str(k)] = str(v)
            self.http_call_result.headers[str(k).lower()] = str(v)

        if err != QNetworkReply.NoError:
            # handle error
            # check if errorString is empty, if so, then set err string as
            # reply dump
            if re.match('(.)*server replied: $', self.reply.errorString()):
                errString = self.reply.errorString() + self.http_call_result.content
            else:
                errString = self.reply.errorString()
            # check if self.http_call_result.status_code is available (client abort
            # does not produce http.status_code)
            if self.http_call_result.status_code:
                msg = "Network error #{0}: {1}".format(
                    self.http_call_result.status_code, errString)
            else:
                msg = "Network error: {0}".format(errString)

            self.http_call_result.reason = msg
            self.http_call_result.ok = False
            self.msg_log(msg)
            # set return exception
            if err == QNetworkReply.TimeoutError:
                self.http_call_result.exception = RequestsExceptionTimeout(msg)

            elif err == QNetworkReply.ConnectionRefusedError:
                self.http_call_result.exception = RequestsExceptionConnectionError(msg)

            elif err == QNetworkReply.OperationCanceledError:
                # request abort by calling NAM.abort() => cancelled by the user
                if self.on_abort:
                    self.http_call_result.exception = RequestsExceptionUserAbort(msg)
                else:
                    self.http_call_result.exception = RequestsException(msg)

            else:
                self.http_call_result.exception = RequestsException(msg)

            # overload exception to the custom exception if available
            if self.exception_class:
                self.http_call_result.exception = self.exception_class(msg)

        else:
            # Handle redirections
            redirectionUrl = self.reply.attribute(QNetworkRequest.RedirectionTargetAttribute)
            if redirectionUrl is not None and redirectionUrl != self.reply.url():
                if redirectionUrl.isRelative():
                    redirectionUrl = self.reply.url().resolved(redirectionUrl)

                msg = "Redirected from '{}' to '{}'".format(
                    self.reply.url().toString(), redirectionUrl.toString())
                self.msg_log(msg)

                self.reply.deleteLater()
                self.reply = None
                
                if self.calling_args['redirections'] <= 0:
                    self.http_call_result.exception = RequestsMaxRedirects("Network error: Exceeded maximum redirects")
                    return
                
                new_request = {}
                request_changes = { 'url': redirectionUrl.toString(),
                                    'redirections': self.calling_args['redirections']-1 }
                new_request.update(self.calling_args)
                new_request.update(request_changes)
                
                # we want to skip the "normal" end of request on a redirect
                return self.request(**new_request)

            # really end request
            else:
                msg = "Network success #{0}".format(self.reply.error())
                self.http_call_result.reason = msg
                self.msg_log(msg)

                ba = self.reply.readAll()
                self.http_call_result.content = bytes(ba)
                self.http_call_result.text = str(ba.data(), encoding='utf-8')
                self.http_call_result.ok = True

        # Let's log the whole response for debugging purposes:
        self.msg_log("Got response %s %s from %s" % \
                    (self.http_call_result.status_code,
                     self.http_call_result.status_message,
                     self.reply.url().toString()))
        for k, v in list(self.http_call_result.headers.items()):
            self.msg_log("%s: %s" % (k, v))
        if len(self.http_call_result.content) < 1024:
            self.msg_log("Payload :\n%s" % self.http_call_result.text)
        else:
            self.msg_log("Payload is > 1 KB ...")

        # clean reply
        if self.reply is not None:
            if self.reply.isRunning():
                self.reply.close()
            self.msg_log("Deleting reply ...")
            # Disconnect all slots
            self.reply.sslErrors.disconnect(self.sslErrors)
            self.reply.finished.disconnect(self.replyFinished)
            self.reply.downloadProgress.disconnect(self.downloadProgress)
            self.reply.deleteLater()
            self.reply = None
        else:
            self.msg_log("Reply was already deleted ...")

    def sslErrors(self, ssl_errors):
        """
        Handle SSL errors, logging them if debug is on and ignoring them
        if disable_ssl_certificate_validation is set.
        """
        if ssl_errors:
            for v in ssl_errors:
                self.msg_log("SSL Error: %s" % v.errorString())
        if self.disable_ssl_certificate_validation:
            self.reply.ignoreSslErrors()

    def abort(self):
        """
        Handle request to cancel HTTP call
        """
        if (self.reply and self.reply.isRunning()):
            self.on_abort = True
            self.reply.abort()
