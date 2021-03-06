import re
import json
from annoying.decorators import render_to

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseRedirect, HttpResponseServerError
from django.shortcuts import render_to_response, get_object_or_404, redirect, get_list_or_404
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt

import settings
from central.forms import OrganizationForm, OrganizationInvitationForm
from central.models import Organization, OrganizationInvitation, DeletionRecord, get_or_create_user_profile, FeedListing, Subscription
from securesync.engine.api_client import SyncSession
from shared.decorators import require_authorized_admin


@render_to("central/homepage.html")
def homepage(request):
    feed = FeedListing.objects.order_by('-posted_date')[:5]
    return {
        "feed": feed,
        "central_contact_email": settings.CENTRAL_CONTACT_EMAIL,
        "wiki_url": settings.CENTRAL_WIKI_URL
    }

@login_required
@render_to("central/org_management.html")
def org_management(request, org_id=None):
    """Management of all organizations for the given user"""

    # get a list of all the organizations this user helps administer
    organizations = get_or_create_user_profile(request.user).get_organizations()

    # add invitation forms to each of the organizations
    for org in organizations.values():
        org.form = OrganizationInvitationForm(initial={"invited_by": request.user})

    # handle a submitted invitation form
    if request.method == "POST":
        form = OrganizationInvitationForm(data=request.POST)
        if form.is_valid():
            # ensure that the current user is a member of the organization to which someone is being invited
            if not form.instance.organization.is_member(request.user):
                raise PermissionDenied("Unfortunately for you, you do not have permission to do that.")
            # send the invitation email, and save the invitation record
            form.instance.send(request)
            form.save()
            return HttpResponseRedirect(reverse("org_management"))
        else: # we need to inject the form into the correct organization, so errors are displayed inline
            for pk,org in organizations.items():
                if org.pk == int(request.POST.get("organization")):
                    org.form = form

    zones = {}
    for org in organizations.values():
        zones[org.pk] = []
        for zone in org.get_zones():
            zones[org.pk].append({
                "id": zone.id,
                "name": zone.name,
                "is_deletable": not zone.has_dependencies(passable_classes=["Organization"]),
            })
    return {
        "title": _("Account administration"),
        "organizations": organizations,
        "zones": zones,
        "HEADLESS_ORG_NAME": Organization.HEADLESS_ORG_NAME,
        "invitations": OrganizationInvitation.objects.filter(email_to_invite=request.user.email)
    }


@csrf_exempt # because we want the front page to cache properly
def add_subscription(request):
    if request.method == "POST":
        sub = Subscription(email=request.POST.get("email"))
        sub.ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get('REMOTE_ADDR', ""))
        sub.save()
        messages.success(request, "A subscription for '%s' was added." % request.POST.get("email"))
    return HttpResponseRedirect(reverse("homepage"))

@login_required
def org_invite_action(request, invite_id):
    invite = OrganizationInvitation.objects.get(pk=invite_id)
    org = invite.organization
    if request.user.email != invite.email_to_invite:
        raise PermissionDenied("It's not nice to force your way into groups.")
    if request.method == "POST":
        data = request.POST
        if data.get("join"):
            messages.success(request, "You have joined " + org.name + " as an admin.")
            org.add_member(request.user)
        if data.get("decline"):
            messages.warning(request, "You have declined to join " + org.name + " as an admin.")
        invite.delete()
    return HttpResponseRedirect(reverse("org_management"))


@require_authorized_admin
def delete_admin(request, org_id, user_id):
    org = Organization.objects.get(pk=org_id)
    admin = org.users.get(pk=user_id)
    if org.owner == admin:
        raise PermissionDenied("The owner of an organization cannot be removed.")
    if request.user == admin:
        raise PermissionDenied("Your personal views are your own, but in this case " +
            "you are not allowed to delete yourself.")
    deletion = DeletionRecord(organization=org, deleter=request.user, deleted_user=admin)
    deletion.save()
    org.users.remove(admin)
    messages.success(request, "You have successfully removed " + admin.username + " as an administrator for " + org.name + ".")
    return HttpResponseRedirect(reverse("org_management"))


@require_authorized_admin
def delete_invite(request, org_id, invite_id):
    org = Organization.objects.get(pk=org_id)
    invite = OrganizationInvitation.objects.get(pk=invite_id)
    deletion = DeletionRecord(organization=org, deleter=request.user, deleted_invite=invite)
    deletion.save()
    invite.delete()
    messages.success(request, "You have successfully revoked the invitation for " + invite.email_to_invite + ".")
    return HttpResponseRedirect(reverse("org_management"))


@require_authorized_admin
@render_to("central/organization_form.html")
def organization_form(request, org_id):
    if org_id != "new":
        org = get_object_or_404(Organization, pk=org_id)
    else:
        org = None
    if request.method == 'POST':
        form = OrganizationForm(data=request.POST, instance=org)
        if form.is_valid():
            # form.instance.owner = form.instance.owner or request.user
            old_org = bool(form.instance.pk)
            form.instance.save(owner=request.user)
            form.instance.users.add(request.user)
            # form.instance.save()
            if old_org:
                return HttpResponseRedirect(reverse("org_management"))
            else:
                return HttpResponseRedirect(reverse("zone_form", kwargs={"zone_id": "new", "org_id": form.instance.pk}) )
    else:
        form = OrganizationForm(instance=org)
    return {
        'form': form
    }

@require_authorized_admin
def delete_organization(request, org_id):
    org = Organization.objects.get(pk=org_id)
    if org.get_zones():
        messages.error(request, "You cannot delete '%s' because it has %d zone(s) affiliated with it." %(org.name, len(org.get_zones())))
    else:
        messages.success(request, "You have successfully deleted " + org.name + ".")
        org.delete()
    return HttpResponseRedirect(reverse("org_management"))


def content_page(request, page, **kwargs):
    context = RequestContext(request)
    context.update(kwargs)
    return render_to_response("central/content/%s.html" % page, context_instance=context)


@render_to("central/glossary.html")
def glossary(request):
    return {}


@login_required
def crypto_login(request):
    """
    Remote admin endpoint, for login to a distributed server (given its IP address; see also securesync/views.py:crypto_login)
    
    An admin login is negotiated using the nonce system inside SyncSession
    """
    if not request.user.is_superuser:
        raise PermissionDenied()
    ip = request.GET.get("ip", "")
    if not ip:
        return HttpResponseNotFound("Please specify an IP (as a GET param).")
    host = "http://%s/" % ip
    client = SyncClient(host=host, require_trusted=False)
    if client.test_connection() != "success":
        return HttpResponse("Unable to connect to a KA Lite server at %s" % host)
    client.start_session()
    if not client.session or not client.session.client_nonce:
        return HttpResponse("Unable to establish a session with KA Lite server at %s" % host)
    return HttpResponseRedirect("%ssecuresync/cryptologin/?client_nonce=%s" % (host, client.session.client_nonce))


def handler_403(request, *args, **kwargs):
    context = RequestContext(request)
    message = None  # Need to retrieve, but can't figure it out yet.

    if request.is_ajax():
        raise PermissionDenied(message)
    else:
        messages.error(request, mark_safe(_("You must be logged in with an account authorized to view this page.")))
        return HttpResponseRedirect(reverse("auth_login") + "?next=" + request.path)

def handler_404(request):
    return HttpResponseNotFound(render_to_string("central/404.html", {}, context_instance=RequestContext(request)))

def handler_500(request):
    return HttpResponseServerError(render_to_string("central/500.html", {}, context_instance=RequestContext(request)))
