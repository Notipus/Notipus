"""Tests for template rendering.

These tests ensure that templates render correctly without errors,
especially custom error templates (404, 500) and key application pages.
"""

import pytest
from core.models import Plan, UserProfile, Workspace, WorkspaceMember
from core.views import custom_404, custom_500
from django.contrib.auth.models import User
from django.template.loader import get_template, render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse


class TemplateLoadingTests(TestCase):
    """Test that templates can be loaded without errors."""

    def test_base_template_loads(self) -> None:
        """Test that the base template can be loaded."""
        template = get_template("core/base.html.j2")
        assert template is not None

    def test_404_template_loads(self) -> None:
        """Test that the 404 template can be loaded."""
        template = get_template("404.html.j2")
        assert template is not None

    def test_500_template_loads(self) -> None:
        """Test that the 500 template can be loaded."""
        template = get_template("500.html.j2")
        assert template is not None

    def test_dashboard_template_loads(self) -> None:
        """Test that the dashboard template can be loaded."""
        template = get_template("core/dashboard.html.j2")
        assert template is not None

    def test_login_template_loads(self) -> None:
        """Test that the login template can be loaded."""
        template = get_template("account/login.html.j2")
        assert template is not None

    def test_signup_template_loads(self) -> None:
        """Test that the signup template can be loaded."""
        template = get_template("account/signup.html.j2")
        assert template is not None


class ErrorTemplateRenderingTests(TestCase):
    """Test that error templates render correctly."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.factory = RequestFactory()

    def test_404_template_renders(self) -> None:
        """Test that the 404 template renders without errors."""
        html = render_to_string("404.html.j2", {})
        assert "404" in html
        assert "Page Not Found" in html
        assert "Go to Dashboard" in html

    def test_500_template_renders(self) -> None:
        """Test that the 500 template renders without errors."""
        html = render_to_string("500.html.j2", {})
        assert "500" in html
        assert "Server Error" in html
        assert "Go to Dashboard" in html

    def test_404_view_returns_correct_status(self) -> None:
        """Test that the custom 404 view returns 404 status code."""
        request = self.factory.get("/nonexistent/")
        response = custom_404(request, Exception("Not found"))
        assert response.status_code == 404
        assert b"Page Not Found" in response.content

    def test_500_view_returns_correct_status(self) -> None:
        """Test that the custom 500 view returns 500 status code."""
        request = self.factory.get("/")
        response = custom_500(request)
        assert response.status_code == 500
        assert b"Server Error" in response.content


class TemplateContextTests(TestCase):
    """Test templates render with various context states."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.client = Client()

        # Create test user
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

        # Create workspace and profile
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )

        self.user_profile = UserProfile.objects.create(
            user=self.user,
            slack_user_id="U123456",
            workspace=self.workspace,
        )

    def test_landing_page_unauthenticated_redirects_to_login(self) -> None:
        """Test landing page redirects unauthenticated users to login."""
        response = self.client.get(reverse("core:landing"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_landing_page_authenticated_redirects_to_dashboard(self) -> None:
        """Test landing page redirects authenticated users to dashboard."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:landing"))
        # Authenticated users are redirected to dashboard
        assert response.status_code == 302
        assert response.url == "/dashboard/"

    def test_dashboard_requires_authentication(self) -> None:
        """Test dashboard redirects unauthenticated users."""
        response = self.client.get(reverse("core:dashboard"))
        assert response.status_code == 302  # Redirect to login

    def test_dashboard_renders_authenticated(self) -> None:
        """Test dashboard renders for authenticated users."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"Dashboard" in response.content

    def test_integrations_page_renders(self) -> None:
        """Test integrations page renders for authenticated users."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:integrations"))
        assert response.status_code == 200


@override_settings(DEBUG=False)
class Error404IntegrationTests(TestCase):
    """Integration tests for 404 error handling."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.client = Client()

    def test_nonexistent_page_returns_404(self) -> None:
        """Test that a non-existent page returns 404 with custom template."""
        response = self.client.get("/this-page-definitely-does-not-exist/")
        assert response.status_code == 404
        # Should render our custom 404 template
        assert b"Page Not Found" in response.content

    def test_nonexistent_api_endpoint_returns_404(self) -> None:
        """Test that a non-existent API endpoint returns 404."""
        response = self.client.get("/api/this-endpoint-does-not-exist/")
        assert response.status_code == 404


class BillingTemplateTests(TestCase):
    """Test billing-related templates render correctly."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.client = Client()

        # Create test user
        self.user = User.objects.create_user(
            username="billinguser",
            email="billing@example.com",
            password="testpass123",
        )

        # Create workspace and profile
        self.workspace = Workspace.objects.create(
            name="Billing Test Org",
            shop_domain="billing.myshopify.com",
            subscription_plan="basic",
            subscription_status="active",
        )

        self.user_profile = UserProfile.objects.create(
            user=self.user,
            slack_user_id="U789012",
            workspace=self.workspace,
        )

        # Create WorkspaceMember to associate user with workspace
        self.workspace_member = WorkspaceMember.objects.create(
            user=self.user,
            workspace=self.workspace,
            role="owner",
            is_active=True,
        )

        # Get or create a plan (plan may already exist from migrations)
        self.plan, _ = Plan.objects.get_or_create(
            name="basic",
            defaults={
                "display_name": "Basic Plan",
                "description": "Basic plan for small teams",
                "price_monthly": 29.00,
                "max_users": 5,
                "max_integrations": 3,
                "max_monthly_notifications": 5000,
                "stripe_price_id_monthly": "price_test123",
                "is_active": True,
            },
        )

    def test_billing_dashboard_renders(self) -> None:
        """Test billing dashboard renders for authenticated users."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:billing_dashboard"))
        assert response.status_code == 200

    def test_select_plan_redirects_workspace_owner_to_upgrade(self) -> None:
        """Users with a workspace change plans on the upgrade page."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:select_plan"))
        assert response.status_code == 302
        assert response.url == reverse("core:upgrade_plan")

    def test_select_plan_renders_for_workspace_less_user(self) -> None:
        """The plan page still renders for a user without a workspace."""
        lone_user = User.objects.create_user(
            username="planless",
            email="planless@example.com",
            password="testpass123",
        )
        self.client.force_login(lone_user)
        response = self.client.get(reverse("core:select_plan"))
        assert response.status_code == 200

    def test_billing_history_renders(self) -> None:
        """Test billing history page renders."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:billing_history"))
        assert response.status_code == 200


class WorkspaceTemplateTests(TestCase):
    """Test workspace-related templates render correctly."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.client = Client()

        # Create test user without workspace
        self.user_no_workspace = User.objects.create_user(
            username="noworkspaceuser",
            email="noworkspace@example.com",
            password="testpass123",
        )

        # Create user with workspace
        self.user_with_workspace = User.objects.create_user(
            username="workspaceuser",
            email="workspace@example.com",
            password="testpass123",
        )

        self.workspace = Workspace.objects.create(
            name="Test Org",
            shop_domain="org.myshopify.com",
        )

        self.user_profile = UserProfile.objects.create(
            user=self.user_with_workspace,
            slack_user_id="U111222",
            workspace=self.workspace,
        )

    def test_create_workspace_renders(self) -> None:
        """Test create workspace page renders."""
        self.client.force_login(self.user_no_workspace)
        response = self.client.get(reverse("core:create_workspace"))
        assert response.status_code == 200

    def test_workspace_settings_renders(self) -> None:
        """Test workspace settings page renders."""
        self.client.force_login(self.user_with_workspace)
        response = self.client.get(reverse("core:workspace_settings"))
        assert response.status_code == 200


class StaticFilesTemplateTests(TestCase):
    """Test that templates correctly reference static files."""

    def test_base_template_includes_css(self) -> None:
        """Test that base template includes CSS reference."""
        html = render_to_string("core/base.html.j2", {})
        assert "main.css" in html

    def test_base_template_includes_logo(self) -> None:
        """Test that base template includes logo reference."""
        html = render_to_string("core/base.html.j2", {})
        assert "notipus-logo.png" in html


class AuthenticationTemplateTests(TestCase):
    """Test authentication-related templates."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.client = Client()

    def test_login_page_renders(self) -> None:
        """Test login page renders correctly."""
        response = self.client.get("/accounts/login/")
        assert response.status_code == 200
        assert b"Login" in response.content or b"Sign In" in response.content

    def test_signup_page_renders(self) -> None:
        """Test signup page renders correctly."""
        response = self.client.get("/accounts/signup/")
        assert response.status_code == 200
        assert b"Sign Up" in response.content or b"Register" in response.content


class AllTemplatesValidationTests(TestCase):
    """Comprehensive tests to validate all Django templates can be loaded and parsed.

    These tests catch template syntax errors like invalid operators (e.g., using
    Python's 'or' instead of Django's |default filter) before deployment.
    """

    @classmethod
    def get_all_template_names(cls) -> list[str]:
        """Discover all template files in the templates directory.

        Returns:
            List of template names relative to templates directory.
        """
        from pathlib import Path

        from django.conf import settings

        template_names: list[str] = []

        # Get template directories from Django settings
        for template_config in settings.TEMPLATES:
            for template_dir in template_config.get("DIRS", []):
                template_path = Path(template_dir)
                if template_path.exists():
                    for template_file in template_path.rglob("*.j2"):
                        # Get relative path from template directory
                        relative_path = template_file.relative_to(template_path)
                        template_names.append(str(relative_path))

            # Also check app directories if APP_DIRS is True
            if template_config.get("APP_DIRS", False):
                from django.apps import apps

                for app_config in apps.get_app_configs():
                    app_template_dir = Path(app_config.path) / "templates"
                    if app_template_dir.exists():
                        for template_file in app_template_dir.rglob("*.j2"):
                            relative_path = template_file.relative_to(app_template_dir)
                            template_names.append(str(relative_path))

        return sorted(set(template_names))

    def test_all_templates_can_be_loaded(self) -> None:
        """Test that all templates can be loaded without syntax errors.

        This catches Django template syntax errors such as:
        - Invalid template tags
        - Invalid filter usage
        - Using Python operators (like 'or') instead of Django template syntax
        - Missing endblock/endif/endfor tags
        - Invalid variable expressions
        """
        from django.template import TemplateSyntaxError
        from django.template.loader import get_template

        template_names = self.get_all_template_names()
        errors: list[tuple[str, str]] = []

        for template_name in template_names:
            try:
                get_template(template_name)
            except TemplateSyntaxError as e:
                errors.append((template_name, str(e)))
            except Exception as e:
                # Catch other loading errors but don't fail on missing context
                if "does not exist" not in str(e).lower():
                    errors.append((template_name, f"Unexpected error: {e}"))

        if errors:
            error_msg = "Template syntax errors found:\n"
            for template_name, error in errors:
                error_msg += f"\n  {template_name}:\n    {error}\n"
            pytest.fail(error_msg)

    def test_all_templates_discovered(self) -> None:
        """Verify that template discovery is working and finds expected templates."""
        template_names = self.get_all_template_names()

        # Should find at least these essential templates
        expected_templates = [
            "core/base.html.j2",
            "core/dashboard.html.j2",
            "account/login.html.j2",
            "account/signup.html.j2",
            "404.html.j2",
            "500.html.j2",
        ]

        for expected in expected_templates:
            assert expected in template_names, f"Expected template {expected} not found"

        # Should have found a reasonable number of templates
        assert len(template_names) >= 10, (
            f"Only found {len(template_names)} templates, expected more"
        )

    def test_standalone_templates_render_with_empty_context(self) -> None:
        """Test standalone templates render with empty context.

        Standalone templates (not extending others) like error pages should
        render without any context variables.
        """
        standalone_templates = [
            "404.html.j2",
            "500.html.j2",
        ]

        for template_name in standalone_templates:
            html = render_to_string(template_name, {})
            assert len(html) > 0, f"Template {template_name} rendered empty"
