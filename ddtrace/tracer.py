import functools
import logging
import threading

from .buffer import ThreadLocalSpanBuffer
from .sampler import AllSampler
from .span import Span
from .writer import AgentWriter


log = logging.getLogger(__name__)


class Tracer(object):
    """Tracer is used to create, sample and submit spans that measure the execution time of sections of code.

    If you're running an application that will serve a single trace per thread,
    you can use the global traced instance:

    >>> from ddtrace import tracer
    >>> tracer.trace("foo").finish()
    """

    DEFAULT_HOSTNAME = 'localhost'
    DEFAULT_PORT = 7777

    def __init__(self):
        """Create a new tracer."""

        # Apply the default configuration
        self.configure(enabled=True, hostname=self.DEFAULT_HOSTNAME, port=self.DEFAULT_PORT, sampler=AllSampler())

        # a list of buffered spans.
        self._spans_lock = threading.Lock()
        self._spans = []

        # track the active span
        self.span_buffer = ThreadLocalSpanBuffer()

        # a collection of registered services by name.
        self._services = {}

        # A hook for local debugging. shouldn't be needed or used
        # in production.
        self.debug_logging = False

    def configure(self, enabled=None, hostname=None, port=None, sampler=None):
        """Configure an existing Tracer the easy way.

        Allow to configure or reconfigure a Tracer instance.

        :param bool enabled: If True, finished traces will be submitted to the API. Otherwise they'll be dropped.
        :param string hostname: Hostname running the Trace Agent
        :param int port: Port of the Trace Agent
        :param object sampler: A custom Sampler instance
        """
        if enabled is not None:
            self.enabled = enabled

        if hostname is not None or port is not None:
            self.writer = AgentWriter(hostname or self.DEFAULT_HOSTNAME, port or self.DEFAULT_PORT)

        if sampler is not None:
            self.sampler = sampler

    def wrap(self, name=None, service=None, resource=None, span_type=None):
        def wrap_decorator(func):
            # TODO elijah: should we include the module name as well?
            span_name = func.__name__ if name is None else name
            @functools.wraps(func)
            def func_wrapper(*args, **kwargs):
                with self.trace(span_name, service=service, resource=resource, span_type=span_type):
                    func(*args, **kwargs)
            return func_wrapper
        return wrap_decorator

    def trace(self, name, service=None, resource=None, span_type=None):
        """Return a span that will trace an operation called `name`.

        :param str name: the name of the operation being traced
        :param str service: the name of the service being traced. If not set,
                            it will inherit the service from it's parent.
        :param str resource: an optional name of the resource being tracked.
        :param str span_type: an optional operation type.

        You must call `finish` on all spans, either directly or with a context
        manager.

        >>> span = tracer.trace("web.request")
            try:
                # do something
            finally:
                span.finish()
        >>> with tracer.trace("web.request") as span:
            # do something

        Trace will store the created span and subsequent child traces will
        become it's children.

        >>> tracer = Tracer()
        >>> parent = tracer.trace("parent")     # has no parent span
        >>> child  = tracer.trace("child")      # is a child of a parent
        >>> child.finish()
        >>> parent.finish()
        >>> parent2 = tracer.trace("parent2")   # has no parent span
        >>> parent2.finish()
        """
        span = None
        parent = self.span_buffer.get()

        if parent:
            # if we have a current span link the parent + child nodes.
            span = Span(
                self,
                name,
                service=(service or parent.service),
                resource=resource,
                span_type=span_type,
                trace_id=parent.trace_id,
                parent_id=parent.span_id,
            )
            span._parent = parent
            span.sampled = parent.sampled
        else:
            span = Span(
                self,
                name,
                service=service,
                resource=resource,
                span_type=span_type,
            )
            self.sampler.sample(span)

        # Note the current trace.
        self.span_buffer.set(span)

        return span

    def current_span(self):
        """Return the current active span or None."""
        return self.span_buffer.get()

    def clear_current_span(self):
        self.span_buffer.set(None)

    def record(self, span):
        """Record the given finished span."""
        spans = []
        with self._spans_lock:
            self._spans.append(span)
            parent = span._parent
            self.span_buffer.set(parent)
            if not parent:
                spans = self._spans
                self._spans = []

        if self.writer and span.sampled:
            self.write(spans)

    def write(self, spans):
        if not spans:
            return  # nothing to do

        if self.debug_logging:
            log.debug("writing %s spans (enabled:%s)", len(spans), self.enabled)
            for span in spans:
                log.debug("\n%s", span.pprint())

        if self.enabled:
            # only submit the spans if we're actually enabled.
            self.writer.write(spans, self._services)

    def set_service_info(self, service, app, app_type):
        """Set the information about the given service.

        :param str service: the internal name of the service (e.g. acme_search, datadog_web)
        :param str app: the off the shelf name of the application (e.g. rails, postgres, custom-app)
        :param str app_type: the type of the application (e.g. db, web)
        """
        self._services[service] = {
            "app" : app,
            "app_type": app_type,
        }

        if self.debug_logging:
            log.debug("set_service_info: service:%s app:%s type:%s",
                service, app, app_type)
