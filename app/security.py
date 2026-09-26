"""公开摘要工作流的角色隔离。

这是一个不依赖外部身份提供方的最小化基于请求头的角色解析，便于在
统一网关后部署：网关鉴权后注入 ``X-Actor-Id`` 与 ``X-Actor-Roles``。

角色与职责（最小授权）：

- ``privacy_officer``：生成预览、阅读隐私影响、提交审批、发起撤回；
- ``approver``：审批隐私影响评估（不得审批自己提交的摘要）；
- ``publisher``：发布已批准摘要、执行撤回；
- ``auditor``：只读全部内部视图，包括已撤回摘要、隐私影响与审计链。

公开查询接口不需要任何角色。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

ROLE_PRIVACY_OFFICER = "privacy_officer"
ROLE_APPROVER = "approver"
ROLE_PUBLISHER = "publisher"
ROLE_AUDITOR = "auditor"

INTERNAL_ROLES = frozenset(
    {
        ROLE_PRIVACY_OFFICER,
        ROLE_APPROVER,
        ROLE_PUBLISHER,
        ROLE_AUDITOR,
    }
)


class AuthorizationError(Exception):
    """操作人缺失或角色不足。"""


@dataclass(frozen=True)
class Actor:
    actor_id: str
    roles: frozenset[str]

    def has(self, role: str) -> bool:
        return role in self.roles

    def has_any(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


def resolve_actor(
    x_actor_id: str | None = Header(default=None),
    x_actor_roles: str | None = Header(default=None),
) -> Actor:
    """从请求头解析操作人；内部接口的依赖项。"""

    actor_id = (x_actor_id or "").strip()
    if not actor_id:
        raise HTTPException(status_code=403, detail="missing X-Actor-Id")
    roles = frozenset(
        part.strip()
        for part in (x_actor_roles or "").split(",")
        if part.strip() in INTERNAL_ROLES
    )
    return Actor(actor_id=actor_id, roles=roles)


def require_roles(actor: Actor, *roles: str) -> None:
    if not actor.has_any(*roles):
        raise AuthorizationError(
            "actor requires one of roles: " + ", ".join(roles)
        )
