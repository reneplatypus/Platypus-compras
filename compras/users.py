"""Manage who can log in. Roles: viewer, preparer (assigns accounts), approver (also approves), admin.

Usage:
    python3 -m compras.users list
    python3 -m compras.users add <email> "<name>" <role>
    python3 -m compras.users disable <email>
"""
import getpass
import os
import sys

from .config import settings
from .review import ROLES
from .store import Store


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    os_user = os.environ.get("SUDO_USER") or getpass.getuser()
    store = Store(settings().db_path, actor=f"cli:{os_user}")
    if argv[:1] == ["list"]:
        for u in store.users():
            print(f"{u['email']:35} {u['name']:28} {u['role']:9} {'activo' if u['active'] else 'inactivo'}")
    elif argv[:1] == ["add"] and len(argv) == 4:
        email, name, role = argv[1:]
        if role not in ROLES:
            sys.exit(f"Rol inválido; use uno de {', '.join(ROLES)}")
        store.upsert_user(email, name, role)
        store.commit()
        print(f"{email} ({role}) activo")
    elif argv[:1] == ["disable"] and len(argv) == 2:
        u = store.user_by_email(argv[1])
        if not u:
            sys.exit("Usuario no encontrado")
        store.upsert_user(u["email"], u["name"], u["role"], active=False)
        store.db.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
        store.commit()
        print(f"{u['email']} desactivado")
    else:
        sys.exit(__doc__)
    store.close()


if __name__ == "__main__":
    main()
