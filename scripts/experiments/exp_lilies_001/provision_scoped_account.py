from __future__ import annotations

import json
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission


HOST = os.environ["EXP_LILIES_ACCOUNT_HOST"]
ROLE = os.environ["EXP_LILIES_ACCOUNT_ROLE"]
USERNAME = os.environ["EXP_LILIES_ACCOUNT_USERNAME"]
PASSWORD = os.environ["EXP_LILIES_ACCOUNT_PASSWORD"]

PERMISSIONS = {
    "paperless": {
        "builder": {
            "view_document",
            "change_document",
            "view_paperlesstask",
            "view_tag",
            "view_customfield",
            "view_correspondent",
            "view_documenttype",
        },
        "verifier": {
            "view_document",
            "view_paperlesstask",
            "view_tag",
            "view_customfield",
            "view_correspondent",
            "view_documenttype",
        },
    },
}

if HOST not in {"paperless", "inventree"} or ROLE not in {"builder", "verifier"}:
    raise RuntimeError("unsupported scoped account host or role")

User = get_user_model()
user, _created = User.objects.get_or_create(username=USERNAME)
user.is_active = True
user.is_staff = False
user.is_superuser = False
user.set_password(PASSWORD)
user.save()

if HOST == "paperless":
    from rest_framework.authtoken.models import Token

    required = PERMISSIONS[HOST][ROLE]
    matches = list(
        Permission.objects.filter(
            content_type__app_label="documents",
            codename__in=required,
        ).order_by(
            "content_type__app_label",
            "codename",
        )
    )
    found = {permission.codename for permission in matches}
    missing = sorted(required - found)
    if missing:
        raise RuntimeError(f"required permissions are unavailable: {missing}")
    user.groups.clear()
    user.user_permissions.set(matches)
    Token.objects.filter(user=user).delete()
    token_value = Token.objects.create(user=user).key
    permission_inventory = sorted(found)
elif HOST == "inventree":
    from users.models import ApiToken, RuleSet, UserProfile

    user.user_permissions.clear()
    # InvenTree treats ``manage.py shell`` as a read-only command, so its
    # post-save signal intentionally skips automatic profile creation.
    # Provision the official profile model explicitly before group mutation.
    UserProfile.objects.get_or_create(user=user)
    group, _ = Group.objects.get_or_create(
        name=f"exp_lilies_{ROLE}",
    )
    related_model_permissions = (
        {
            ("order", "change_purchaseorder"),
        }
        if ROLE == "builder"
        else set()
    )
    related_permission_rows = list(
        Permission.objects.filter(
            content_type__app_label__in={
                app_label
                for app_label, _codename in related_model_permissions
            },
            codename__in={
                codename
                for _app_label, codename in related_model_permissions
            },
        ).order_by(
            "content_type__app_label",
            "codename",
        )
    )
    found_related_permissions = {
        (
            permission.content_type.app_label,
            permission.codename,
        )
        for permission in related_permission_rows
    }
    missing_related_permissions = sorted(
        related_model_permissions - found_related_permissions
    )
    if missing_related_permissions:
        raise RuntimeError(
            "required related-model permissions are unavailable: "
            f"{missing_related_permissions}"
        )
    group.permissions.set(related_permission_rows)
    rules = {
        "part": {
            "can_view": True,
            "can_add": False,
            "can_change": False,
            "can_delete": False,
        },
        "purchase_order": {
            "can_view": True,
            "can_add": False,
            "can_change": ROLE == "builder",
            "can_delete": False,
        },
    }
    RuleSet.objects.filter(group=group).exclude(name__in=rules).delete()
    for name, values in rules.items():
        rule, _ = RuleSet.objects.update_or_create(
            group=group,
            name=name,
            defaults=values,
        )
        rule.save()
    user.groups.set([group])
    token_name = f"{HOST}-{ROLE}-{USERNAME}"
    ApiToken.objects.filter(user=user, name=token_name).delete()
    token_value = ApiToken.objects.create(
        user=user,
        name=token_name,
    ).key
    permission_inventory = [
        (
            f"ruleset:{name}:"
            + ",".join(
                permission
                for permission in ("view", "add", "change", "delete")
                if values[f"can_{permission}"]
            )
        )
        for name, values in sorted(rules.items())
    ]
    permission_inventory.extend(
        f"django:{app_label}.{codename}"
        for app_label, codename in sorted(found_related_permissions)
    )
else:
    raise RuntimeError("unsupported scoped account host")

print(
    json.dumps(
        {
            "schema_version": "1.0",
            "host": HOST,
            "role": ROLE,
            "username": USERNAME,
            "user_id": user.pk,
            "permission_codenames": permission_inventory,
            "token": token_value,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
)
