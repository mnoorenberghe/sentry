"""
sentry.web.views
~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2012 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""
import datetime
import logging

from django.core.urlresolvers import reverse
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseBadRequest, \
  HttpResponseForbidden, HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.vary import vary_on_cookie
from django.views.generic.base import View as BaseView
from sentry.conf import settings
from sentry.constants import MEMBER_USER
from sentry.coreapi import project_from_auth_vars, \
  decode_and_decompress_data, safely_load_json_string, validate_data, \
  insert_data_to_database, APIError, extract_auth_vars
from sentry.exceptions import InvalidData
from sentry.models import Group, GroupBookmark, Project, ProjectCountByMinute, View, FilterValue
from sentry.templatetags.sentry_helpers import with_metadata
from sentry.utils import json
from sentry.utils.cache import cache
from sentry.utils.db import has_trending
from sentry.utils.javascript import to_json
from sentry.utils.http import is_valid_origin, apply_access_control_headers, \
  get_origins
from sentry.web.decorators import has_access
from sentry.web.frontend.groups import _get_group_list
from sentry.web.helpers import render_to_response, render_to_string, get_project_list

error_logger = logging.getLogger('sentry.errors.api.http')
logger = logging.getLogger('sentry.api.http')


def transform_groups(request, group_list, template='sentry/partial/_group.html'):
    return [
        {
            'id': m.pk,
            'html': render_to_string(template, {
                'group': m,
                'request': request,
                'metadata': d,
            }).strip(),
            'title': m.message_top(),
            'message': m.error(),
            'level': m.get_level_display(),
            'logger': m.logger,
            'count': m.times_seen,
            'is_public': m.is_public,
            'score': getattr(m, 'sort_value', None),
        }
        for m, d in with_metadata(group_list, request)
    ]


class Auth(object):
    def __init__(self, auth_vars):
        self.client = auth_vars.get('client')


class APIView(BaseView):
    http_method_names = ['options']

    def _get_project_from_id(self, project_id):
        if project_id:
            if project_id.isdigit():
                lookup_kwargs = {'id': int(project_id)}
            else:
                lookup_kwargs = {'slug': project_id}

            try:
                return Project.objects.get_from_cache(**lookup_kwargs)
            except Project.DoesNotExist:
                raise APIError('Invalid project_id: %r' % project_id)
        return None

    def _parse_header(self, request, project):
        try:
            auth_vars = extract_auth_vars(request)
        except (IndexError, ValueError):
            raise APIError('Invalid auth header')

        if not auth_vars:
            raise APIError('Client/server version mismatch: Unsupported client')

        server_version = auth_vars.get('sentry_version', '1.0')
        client = auth_vars.get('sentry_client', request.META.get('HTTP_USER_AGENT'))

        if server_version not in ('2.0', '3'):
            raise APIError('Client/server version mismatch: Unsupported protocol version (%s)' % server_version)

        if not client:
            raise APIError('Client request error: Missing client version identifier')

        return auth_vars

    @csrf_exempt
    def dispatch(self, request, project_id=None, *args, **kwargs):
        response = self._dispatch(request, project_id=project_id, *args, **kwargs)
        # Set X-Sentry-Error as in many cases it is easier to inspect the headers
        if response.status_code != 200:
            response['X-Sentry-Error'] = response.content[:200]  # safety net on content length
        return response

    def _dispatch(self, request, project_id=None, *args, **kwargs):
        try:
            project = self._get_project_from_id(project_id)
        except APIError, e:
            return HttpResponse(str(e), status=400)

        try:
            auth_vars = self._parse_header(request, project)
        except APIError, e:
            return HttpResponse(str(e), status=400)

        try:
            project_ = project_from_auth_vars(auth_vars)
        except APIError, error:
            logger.info('Project %r raised API error: %s', project.slug, error, extra={
                'request': request,
            }, exc_info=True)
            return HttpResponse(unicode(error.msg), status=error.http_status)

        # Legacy API was /api/store/ and the project ID was only available elsewhere
        if not project:
            if not project_:
                return HttpResponse('Unable to identify project', status=400)
            project = project_
        elif project_ != project:
            return HttpResponse('Project ID mismatch', status=400)

        origin = request.META.get('HTTP_ORIGIN', None)
        if origin is not None and not is_valid_origin(origin, project):
            return HttpResponse('Invalid origin: %r' % origin, status=400)

        auth = Auth(auth_vars)

        try:
            response = super(APIView, self).dispatch(request, project=project, auth=auth, **kwargs)

        except APIError, error:
            logger.info('Project %r raised API error: %s', project.slug, error, extra={
                'request': request,
            }, exc_info=True)
            response = HttpResponse(unicode(error.msg), status=error.http_status)

        response = apply_access_control_headers(response, origin)

        return response

    # XXX: backported from Django 1.5
    def _allowed_methods(self):
        return [m.upper() for m in self.http_method_names if hasattr(self, m)]

    def options(self, request, *args, **kwargs):
        response = HttpResponse()
        response['Allow'] = ', '.join(self._allowed_methods())
        response['Content-Length'] = '0'
        return response


class StoreView(APIView):
    """
    The primary endpoint for storing new events.

    This will validate the client's authentication and data, and if
    successfull pass on the payload to the internal database handler.

    Authentication works in three flavors:

    1. Explicit signed requests

       These are implemented using the documented signed request protocol, and
       require an authentication header which is signed using with the project
       member's secret key.

    2. CORS Secured Requests

       Generally used for communications with client-side platforms (such as
       JavaScript in the browser), they require a standard header, excluding
       the signature and timestamp requirements, and must be listed in the
       origins for the given project (or the global origins).

    3. Implicit trusted requests

       Used by the Sentry core, they are only available from same-domain requests
       and do not require any authentication information. They only require that
       the user be authenticated, and a project_id be sent in the GET variables.

    """
    http_method_names = ['head', 'post', 'options']

    @never_cache
    def post(self, request, project, auth, **kwargs):
        data = request.raw_post_data

        if not data.startswith('{'):
            data = decode_and_decompress_data(data)
        data = safely_load_json_string(data)

        try:
            validate_data(project, data, auth.client)
        except InvalidData, e:
            raise APIError(u'Invalid data: %s (%s)' % (unicode(e), type(e)))

        insert_data_to_database(data)

        logger.info('New event from project %r (id=%s)', project.slug, data['event_id'])

        return HttpResponse()


@csrf_exempt
@has_access
@never_cache
def notification(request, project):
    return render_to_response('sentry/partial/_notification.html', request.GET)


@csrf_exempt
@has_access
@never_cache
def poll(request, project):
    offset = 0
    limit = settings.MESSAGES_PER_PAGE

    view_id = request.GET.get('view_id')
    if view_id:
        try:
            view = View.objects.get_from_cache(pk=view_id)
        except View.DoesNotExist:
            return HttpResponseBadRequest()
    else:
        view = None

    response = _get_group_list(
        request=request,
        project=project,
        view=view,
    )

    event_list = response['event_list']
    event_list = list(event_list[offset:limit])

    data = to_json(event_list, request)

    response = HttpResponse(data)
    response['Content-Type'] = 'application/json'
    return response


@csrf_exempt
@has_access(MEMBER_USER)
@never_cache
def resolve(request, project):
    gid = request.REQUEST.get('gid')
    if not gid:
        return HttpResponseForbidden()
    try:
        group = Group.objects.get(pk=gid)
    except Group.DoesNotExist:
        return HttpResponseForbidden()

    now = timezone.now()

    Group.objects.filter(pk=group.pk).update(
        status=1,
        resolved_at=now,
    )
    group.status = 1
    group.resolved_at = now

    data = transform_groups(request, [group])

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'
    return response


@csrf_exempt
@has_access(MEMBER_USER)
@never_cache
def make_group_public(request, project, group_id):
    try:
        group = Group.objects.get(pk=group_id)
    except Group.DoesNotExist:
        return HttpResponseForbidden()

    group.update(is_public=True)

    data = transform_groups(request, [group])

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'
    return response


@csrf_exempt
@has_access(MEMBER_USER)
@never_cache
def make_group_private(request, project, group_id):
    try:
        group = Group.objects.get(pk=group_id)
    except Group.DoesNotExist:
        return HttpResponseForbidden()

    group.update(is_public=False)

    data = transform_groups(request, [group])

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'
    return response


@csrf_exempt
@has_access(MEMBER_USER)
@never_cache
def remove_group(request, project, group_id):
    try:
        group = Group.objects.get(pk=group_id)
    except Group.DoesNotExist:
        return HttpResponseForbidden()

    group.delete()

    if request.is_ajax():
        response = HttpResponse('{}')
        response['Content-Type'] = 'application/json'
    else:
        response = HttpResponseRedirect(reverse('sentry', args=[project.slug]))
    return response


@csrf_exempt
@has_access
@never_cache
def bookmark(request, project):
    gid = request.REQUEST.get('gid')
    if not gid:
        return HttpResponseForbidden()

    if not request.user.is_authenticated():
        return HttpResponseForbidden()

    try:
        group = Group.objects.get(pk=gid)
    except Group.DoesNotExist:
        return HttpResponseForbidden()

    gb, created = GroupBookmark.objects.get_or_create(
        project=group.project,
        user=request.user,
        group=group,
    )
    if not created:
        gb.delete()

    response = HttpResponse(json.dumps({'bookmarked': created}))
    response['Content-Type'] = 'application/json'
    return response


@csrf_exempt
@has_access(MEMBER_USER)
@never_cache
def clear(request, project):
    view_id = request.GET.get('view_id')
    if view_id:
        try:
            view = View.objects.get_from_cache(pk=view_id)
        except View.DoesNotExist:
            return HttpResponseBadRequest()
    else:
        view = None

    response = _get_group_list(
        request=request,
        project=project,
        view=view,
    )

    event_list = response['event_list']
    event_list.update(status=1)

    data = []
    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'
    return response


@vary_on_cookie
@csrf_exempt
@has_access
def chart(request, project=None):
    gid = request.REQUEST.get('gid')
    days = int(request.REQUEST.get('days', '90'))
    if gid:
        try:
            group = Group.objects.get(pk=gid)
        except Group.DoesNotExist:
            return HttpResponseForbidden()

        data = Group.objects.get_chart_data(group, max_days=days)
    elif project:
        data = Project.objects.get_chart_data(project, max_days=days)
    else:
        cache_key = 'api.chart:user=%s,days=%s' % (request.user.id, days)

        data = cache.get(cache_key)
        if data is None:
            project_list = get_project_list(request.user).values()
            data = Project.objects.get_chart_data_for_group(project_list, max_days=days)
            cache.set(cache_key, data, 300)

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'
    return response


@never_cache
@csrf_exempt
@has_access
def get_group_trends(request, project=None):
    minutes = int(request.REQUEST.get('minutes', 15))
    limit = min(100, int(request.REQUEST.get('limit', 10)))

    if project:
        project_dict = {project.pk: project}
    else:
        project_dict = get_project_list(request.user)

    base_qs = Group.objects.filter(
        project__in=project_dict.keys(),
        status=0,
    )

    if has_trending():
        group_list = list(Group.objects.get_accelerated(base_qs, minutes=(
            minutes
        ))[:limit])
    else:
        cutoff = datetime.timedelta(minutes=minutes)
        cutoff_dt = timezone.now() - cutoff

        group_list = list(base_qs.filter(
            last_seen__gte=cutoff_dt
        ).order_by('-score')[:limit])

    for group in group_list:
        group._project_cache = project_dict.get(group.project_id)

    data = to_json(group_list, request)

    response = HttpResponse(data)
    response['Content-Type'] = 'application/json'

    return response


@never_cache
@csrf_exempt
@has_access
def get_new_groups(request, project=None):
    minutes = int(request.REQUEST.get('minutes', 15))
    limit = min(100, int(request.REQUEST.get('limit', 10)))

    if project:
        project_dict = {project.id: project}
    else:
        project_dict = get_project_list(request.user)

    cutoff = datetime.timedelta(minutes=minutes)
    cutoff_dt = timezone.now() - cutoff

    group_list = list(Group.objects.filter(
        project__in=project_dict.keys(),
        status=0,
        active_at__gte=cutoff_dt,
    ).order_by('-score')[:limit])

    for group in group_list:
        group._project_cache = project_dict.get(group.project_id)

    data = to_json(group_list, request)

    response = HttpResponse(data)
    response['Content-Type'] = 'application/json'

    return response


@never_cache
@csrf_exempt
@has_access
def get_resolved_groups(request, project=None):
    minutes = int(request.REQUEST.get('minutes', 15))
    limit = min(100, int(request.REQUEST.get('limit', 10)))

    if project:
        project_list = [project]
    else:
        project_list = get_project_list(request.user).values()

    cutoff = datetime.timedelta(minutes=minutes)
    cutoff_dt = timezone.now() - cutoff

    group_list = Group.objects.filter(
        project__in=project_list,
        status=1,
        resolved_at__gte=cutoff_dt,
    ).select_related('project').order_by('-score')[:limit]

    data = to_json(group_list, request)

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'

    return response


@never_cache
@csrf_exempt
@has_access
def get_stats(request, project=None):
    minutes = int(request.REQUEST.get('minutes', 15))

    if project:
        project_list = [project]
    else:
        project_list = get_project_list(request.user).values()

    cutoff = datetime.timedelta(minutes=minutes)
    cutoff_dt = timezone.now() - cutoff

    num_events = ProjectCountByMinute.objects.filter(
        project__in=project_list,
        date__gte=cutoff_dt,
    ).aggregate(t=Sum('times_seen'))['t'] or 0

    # XXX: This is too slow if large amounts of groups are resolved
    num_resolved = Group.objects.filter(
        project__in=project_list,
        status=1,
        resolved_at__gte=cutoff_dt,
    ).aggregate(t=Sum('times_seen'))['t'] or 0

    data = {
        'events': num_events,
        'resolved': num_resolved,
    }

    response = HttpResponse(json.dumps(data))
    response['Content-Type'] = 'application/json'

    return response


@never_cache
@csrf_exempt
@has_access
def search_tags(request, project):
    limit = min(100, int(request.GET.get('limit', 10)))
    name = request.GET['name']
    query = request.GET['query']

    results = list(FilterValue.objects.filter(
        project=project,
        key=name,
        value__icontains=query,
    ).values_list('value', flat=True).order_by('value')[:limit])

    response = HttpResponse(json.dumps({
        'results': results,
    }))
    response['Content-Type'] = 'application/json'

    return response


def crossdomain_xml_index(request):
    response = HttpResponse("""<cross-domain-policy>
        <site-control permitted-cross-domain-policies="all"></site-control>
    </cross-domain-policy>""")
    response['Content-Type'] = 'application/xml'
    return response


@has_access
def crossdomain_xml(request, project):
    origin_list = get_origins(project)
    if origin_list == '*':
        origin_list = [origin_list]

    response = render_to_response('sentry/crossdomain.xml', {
        'origin_list': origin_list
    }, request)
    response['Content-Type'] = 'application/xml'

    return response
