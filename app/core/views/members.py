"""Member management views for workspace team administration.

This module provides views for:
- Listing workspace members and pending invitations
- Inviting new members
- Removing members
- Changing member roles
- Accepting invitations
"""

import logging
from uuid import UUID

from core.models import WorkspaceInvitation, WorkspaceMember
from core.permissions import (
    admin_required,
    can_change_role,
    can_invite_user,
    can_remove_member,
    get_remaining_seats,
)
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

logger = logging.getLogger(__name__)


def send_invitation_email(
    invitation: WorkspaceInvitation,
    invite_url: str,
) -> bool:
    """Send an invitation email to the invited user.

    Args:
        invitation: The WorkspaceInvitation instance.
        invite_url: The full URL to accept the invitation.

    Returns:
        True if email was sent successfully, False otherwise.
    """
    workspace_name = invitation.workspace.name
    inviter_name = invitation.invited_by.get_full_name() or invitation.invited_by.email
    role = invitation.role

    subject = f"You've been invited to join {workspace_name} on Notipus"

    # Plain text version
    text_message = f"""Hi,

{inviter_name} has invited you to join {workspace_name} on Notipus as a {role}.

Click the link below to accept the invitation:
{invite_url}

This invitation will expire in 7 days.

If you weren't expecting this invitation, you can safely ignore this email.

- The Notipus Team
"""

    # HTML version
    html_message = render_to_string(
        "core/emails/invitation.html.j2",
        {
            "invitation": invitation,
            "invite_url": invite_url,
            "inviter_name": inviter_name,
        },
    )

    try:
        send_mail(
            subject=subject,
            message=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invitation.email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Invitation email sent to {invitation.email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invitation email to {invitation.email}: {e}")
        return False


@login_required
@admin_required
def members_list(request: HttpRequest) -> HttpResponse:
    """Display list of workspace members and pending invitations.

    Args:
        request: The HTTP request with workspace_member attached by decorator.

    Returns:
        Rendered members list page.
    """
    workspace = request.workspace  # type: ignore[attr-defined]
    current_member = request.workspace_member  # type: ignore[attr-defined]

    # Get all members
    members = WorkspaceMember.objects.filter(
        workspace=workspace, is_active=True
    ).select_related("user")

    # Get pending invitations
    pending_invitations = WorkspaceInvitation.objects.filter(
        workspace=workspace,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).select_related("invited_by")

    # Check if can invite more users
    can_invite, invite_message = can_invite_user(workspace)
    remaining_seats = get_remaining_seats(workspace)

    context = {
        "workspace": workspace,
        "members": members,
        "pending_invitations": pending_invitations,
        "current_member": current_member,
        "can_invite": can_invite,
        "invite_message": invite_message,
        "remaining_seats": remaining_seats,
        "role_choices": WorkspaceMember.ROLE_CHOICES,
    }

    return render(request, "core/members.html.j2", context)


@login_required
@admin_required
@require_POST
def invite_member(request: HttpRequest) -> HttpResponse:
    """Create and send an invitation to a new member.

    Args:
        request: The HTTP request with email and role in POST data.

    Returns:
        Redirect to members list.
    """
    workspace = request.workspace  # type: ignore[attr-defined]
    current_member = request.workspace_member  # type: ignore[attr-defined]

    email = request.POST.get("email", "").strip().lower()
    role = request.POST.get("role", "user")

    # Validate email
    if not email:
        messages.error(request, "Please enter an email address.")
        return redirect("core:members_list")

    # Validate role
    valid_roles = [r[0] for r in WorkspaceMember.ROLE_CHOICES]
    if role not in valid_roles:
        messages.error(request, "Invalid role selected.")
        return redirect("core:members_list")

    # Admins cannot invite owners
    if role == "owner" and current_member.role != "owner":
        messages.error(request, "Only owners can invite new owners.")
        return redirect("core:members_list")

    # Check plan limits
    can_invite, message = can_invite_user(workspace)
    if not can_invite:
        messages.error(request, message)
        return redirect("core:members_list")

    # Check if user is already a member
    existing_member = WorkspaceMember.objects.filter(
        workspace=workspace, user__email=email, is_active=True
    ).first()
    if existing_member:
        messages.error(request, f"{email} is already a member of this workspace.")
        return redirect("core:members_list")

    # Check for existing pending invitation
    existing_invitation = WorkspaceInvitation.objects.filter(
        workspace=workspace,
        email=email,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if existing_invitation:
        messages.warning(request, f"An invitation has already been sent to {email}.")
        return redirect("core:members_list")

    # Create invitation
    invitation = WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email,
        role=role,
        invited_by=request.user,
    )

    # Build the invitation URL and send the email
    invite_url = request.build_absolute_uri(
        f"/workspace/invitation/{invitation.token}/accept/"
    )
    logger.info(f"Invitation created for {email}: {invite_url}")

    email_sent = send_invitation_email(invitation, invite_url)

    if email_sent:
        messages.success(
            request,
            f"Invitation sent to {email}. They will receive an email with "
            f"instructions to join the workspace.",
        )
    else:
        messages.warning(
            request,
            f"Invitation created for {email}, but the email could not be sent. "
            f"Please share the invitation link manually.",
        )

    return redirect("core:members_list")


@login_required
@admin_required
@require_POST
def remove_member(request: HttpRequest, member_id: int) -> HttpResponse:
    """Remove a member from the workspace.

    Args:
        request: The HTTP request.
        member_id: The ID of the member to remove.

    Returns:
        Redirect to members list.
    """
    workspace = request.workspace  # type: ignore[attr-defined]
    current_member = request.workspace_member  # type: ignore[attr-defined]

    # Get target member
    target_member = get_object_or_404(
        WorkspaceMember, id=member_id, workspace=workspace, is_active=True
    )

    # Cannot remove yourself
    if target_member.id == current_member.id:
        messages.error(request, "You cannot remove yourself from the workspace.")
        return redirect("core:members_list")

    # Check permission
    if not can_remove_member(current_member, target_member):
        messages.error(request, "You don't have permission to remove this member.")
        return redirect("core:members_list")

    # Soft delete by marking inactive
    target_member.is_active = False
    target_member.save(update_fields=["is_active"])

    messages.success(
        request, f"{target_member.user.email} has been removed from the workspace."
    )
    assert isinstance(request.user, User)
    logger.info(
        f"Member {target_member.user.email} removed from workspace "
        f"{workspace.name} by {request.user.email}"
    )
    return redirect("core:members_list")


@login_required
@admin_required
@require_POST
def change_role(request: HttpRequest, member_id: int) -> HttpResponse:
    """Change a member's role in the workspace.

    Args:
        request: The HTTP request with new_role in POST data.
        member_id: The ID of the member to update.

    Returns:
        Redirect to members list.
    """
    workspace = request.workspace  # type: ignore[attr-defined]
    current_member = request.workspace_member  # type: ignore[attr-defined]

    new_role = request.POST.get("role", "").strip()

    # Validate role
    valid_roles = [r[0] for r in WorkspaceMember.ROLE_CHOICES]
    if new_role not in valid_roles:
        messages.error(request, "Invalid role selected.")
        return redirect("core:members_list")

    # Get target member
    target_member = get_object_or_404(
        WorkspaceMember, id=member_id, workspace=workspace, is_active=True
    )

    # Cannot change your own role
    if target_member.id == current_member.id:
        messages.error(request, "You cannot change your own role.")
        return redirect("core:members_list")

    # Check permission
    if not can_change_role(current_member, target_member, new_role):
        messages.error(request, "You don't have permission to make this role change.")
        return redirect("core:members_list")

    # Update role
    old_role = target_member.role
    target_member.role = new_role
    target_member.save(update_fields=["role"])

    messages.success(
        request,
        f"{target_member.user.email}'s role changed from {old_role} to {new_role}.",
    )
    assert isinstance(request.user, User)
    logger.info(
        f"Member {target_member.user.email} role changed from {old_role} to "
        f"{new_role} in workspace {workspace.name} by {request.user.email}"
    )
    return redirect("core:members_list")


@login_required
@admin_required
@require_POST
def cancel_invitation(request: HttpRequest, invitation_id: int) -> HttpResponse:
    """Cancel a pending invitation.

    Args:
        request: The HTTP request.
        invitation_id: The ID of the invitation to cancel.

    Returns:
        Redirect to members list.
    """
    workspace = request.workspace  # type: ignore[attr-defined]

    invitation = get_object_or_404(
        WorkspaceInvitation,
        id=invitation_id,
        workspace=workspace,
        accepted_at__isnull=True,
    )

    email = invitation.email
    invitation.delete()

    assert isinstance(request.user, User)
    messages.success(request, f"Invitation to {email} has been cancelled.")
    logger.info(
        f"Invitation to {email} cancelled for workspace {workspace.name} "
        f"by {request.user.email}"
    )
    return redirect("core:members_list")


@require_GET
def accept_invitation(request: HttpRequest, token: UUID) -> HttpResponse:
    """Accept a workspace invitation.

    This view can be accessed without authentication. If the user is not
    logged in, they will be prompted to log in or sign up first.

    Args:
        request: The HTTP request.
        token: The invitation token UUID.

    Returns:
        Rendered acceptance page or redirect to dashboard.
    """
    invitation = get_object_or_404(WorkspaceInvitation, token=token)

    # Check if invitation is expired
    if invitation.expires_at < timezone.now():
        messages.error(
            request, "This invitation has expired. Please request a new one."
        )
        return redirect("account_login")

    # Check if already accepted
    if invitation.accepted_at is not None:
        messages.info(request, "This invitation has already been accepted.")
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return redirect("account_login")

    # If user is not authenticated, show login/signup page with invitation context
    if not request.user.is_authenticated:
        context = {
            "invitation": invitation,
            "workspace": invitation.workspace,
        }
        return render(request, "core/accept_invitation.html.j2", context)

    # User is authenticated - process the invitation
    return _process_invitation_acceptance(request, invitation)


@login_required
@require_POST
def confirm_accept_invitation(request: HttpRequest, token: UUID) -> HttpResponse:
    """Confirm and process invitation acceptance.

    Args:
        request: The HTTP request.
        token: The invitation token UUID.

    Returns:
        Redirect to dashboard on success.
    """
    invitation = get_object_or_404(WorkspaceInvitation, token=token)

    # Validate invitation
    if invitation.expires_at < timezone.now():
        messages.error(
            request, "This invitation has expired. Please request a new one."
        )
        return redirect("core:dashboard")

    if invitation.accepted_at is not None:
        messages.info(request, "This invitation has already been accepted.")
        return redirect("core:dashboard")

    return _process_invitation_acceptance(request, invitation)


def _process_invitation_acceptance(
    request: HttpRequest, invitation: WorkspaceInvitation
) -> HttpResponse:
    """Process the acceptance of an invitation.

    Args:
        request: The HTTP request.
        invitation: The invitation to accept.

    Returns:
        Redirect to dashboard.
    """
    assert isinstance(request.user, User)
    user = request.user
    workspace = invitation.workspace

    # Check if email matches
    if user.email.lower() != invitation.email.lower():
        messages.error(
            request,
            f"This invitation was sent to {invitation.email}. "
            f"Please log in with that email address.",
        )
        return redirect("account_login")

    # Check if user is already a member
    existing_member = WorkspaceMember.objects.filter(
        workspace=workspace, user=user
    ).first()

    if existing_member:
        if existing_member.is_active:
            messages.info(request, "You are already a member of this workspace.")
        else:
            # Reactivate membership
            existing_member.is_active = True
            existing_member.role = invitation.role
            existing_member.save(update_fields=["is_active", "role"])
            messages.success(request, f"Welcome back to {workspace.name}!")
    else:
        # Create new membership
        WorkspaceMember.objects.create(
            user=user,
            workspace=workspace,
            role=invitation.role,
        )
        messages.success(
            request, f"Welcome to {workspace.name}! You've joined as {invitation.role}."
        )

    # Mark invitation as accepted
    with transaction.atomic():
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at"])

    logger.info(
        f"User {user.email} accepted invitation to workspace {workspace.name} "
        f"as {invitation.role}"
    )
    return redirect("core:dashboard")
