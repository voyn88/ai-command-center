"""Who is at this console: the Streamlit identity gate (VOYN-W0-AICC-CONSOLE-NO-AUTH).

The console had no authentication layer (see ``ARCHITECTURE.md`` "Streamlit
host") yet performs privileged git/gh and subprocess operations, which the
pre-existing localhost-binding controls only ever compensated for, never
fixed. This module is the fix: it reuses the *same* identity authority
already accepted for the mutating HTTP surface
(``command_center.http_auth.identity``) rather than building a second one —
the platform says who is calling, ``command_center.http_auth.authz`` says
what they may do, and this module is only the Streamlit-shaped glue between
the two, following ``docs/AIOS_BOUNDARY.md``'s "consumer, not engine" shape.

Two distinct checks, deliberately not collapsed into one:

* :func:`require_identity` — a *session-scoped* gate. Streamlit reruns this
  entire script on every widget interaction (a page navigation, a filter
  change), unlike a discrete HTTP request; re-verifying against the platform
  on every rerun would mean a network round trip per click for no security
  benefit navigation doesn't need. So the operator signs in once per browser
  session and the resulting :class:`~command_center.http_auth.identity.Principal`
  is cached in ``st.session_state`` for the rest of that session. This
  establishes *who is sitting at this console* — it does not, by itself,
  authorize any privileged action.
* :func:`require_console_operation` — called immediately before a privileged
  action acts (a subprocess launch today; more as
  ``VOYN-W0-AICC-STREAMLIT-AUTHZ-DEEP-01`` gates deeper call sites). It
  re-verifies the credential fresh, with no cache, exactly like
  ``http_auth.routing.enforce`` does for a mutating HTTP request — a revoked
  credential must be refused on the very next privileged act, not merely the
  next login, and a cache here would reintroduce the staleness window the
  HTTP boundary was written to close.

Both begin by asking :func:`_in_script_run`, and they answer it differently
on purpose. ``app.py`` is a module: importing it (a test collecting
``app.create_task``, a doc tool) executes the top-level gate call with no
``ScriptRunContext``. Nothing is rendered to anyone there, so
:func:`require_identity` has nothing to gate and emits nothing — emitting a
login form anyway is not a harmless no-op, because a ``with st.form(...)``
that enqueues nothing gets handed Streamlit's process-global ``main_dg``
instead of a child block and stamps the form id onto it permanently, which
makes every later widget in the process look like it is inside that form.
:func:`require_console_operation` has the opposite problem — something
privileged is about to happen and every refusal primitive it could use is a
no-op — so it raises :class:`ConsoleGateUnavailable` instead.
"""

from __future__ import annotations

from typing import Literal, NoReturn

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from command_center.http_auth import authz
from command_center.http_auth.identity import PlatformUnavailable, Principal
from command_center.http_auth.identity import _whoami as whoami
from command_center.ui import theme

#: Session-state keys. Leading underscore keeps them out of any widget's own
#: namespace and signals "internal to this module" the way the rest of the
#: console spells session-private state (see e.g. ``agent_launcher``'s
#: ``confirm_key`` pattern).
_PRINCIPAL_KEY = "_console_principal"
_TOKEN_KEY = "_console_token"


class ConsoleGateUnavailable(RuntimeError):
    """A privileged console action was attempted where the gate cannot refuse.

    Raised only outside a Streamlit script run — see :func:`_in_script_run`.
    Deliberately an exception rather than an ``st.error``: the whole reason
    this state is dangerous is that the Streamlit refusal primitives do
    nothing there, so the refusal has to be one Python itself enforces.
    """


def _in_script_run() -> bool:
    """Is this executing inside a real Streamlit script run?

    ``app.py`` is a module, so anything that merely *imports* it — a test
    collecting ``app.create_task``, a doc tool, ``python app.py`` — executes
    its top-level gate call with no ``ScriptRunContext``. That is not a user
    session: nothing is rendered to anyone, and every Streamlit control-flow
    primitive this module relies on is a documented no-op there. ``st.stop()``
    is the decisive one, and this is the exact predicate it tests before it
    acts, so the two agree by construction: without a context *and* the
    request channel that carries the stop, a gate that "refuses" by calling
    ``st.stop()`` does not refuse at all, and the code after it runs.
    """
    ctx = get_script_run_ctx(suppress_warning=True)
    return ctx is not None and ctx.script_requests is not None


def _authenticated_principal() -> Principal | None:
    principal = st.session_state.get(_PRINCIPAL_KEY)
    token = st.session_state.get(_TOKEN_KEY)
    if isinstance(principal, Principal) and isinstance(token, str) and token:
        return principal
    return None


def _sign_out(*, message: str) -> NoReturn:
    st.session_state.pop(_PRINCIPAL_KEY, None)
    st.session_state.pop(_TOKEN_KEY, None)
    st.error(message)
    st.stop()


def _render_login(*, page_title: str, page_icon: str) -> NoReturn:
    theme.inject_global_css()
    st.title(f"{page_icon} {page_title}")
    st.caption(
        "Эта консоль выполняет привилегированные операции (git, gh, запуск "
        "процессов) и требует действующего платформенного токена."
    )
    with st.form("console_login_form"):
        token_input = st.text_input(
            "Платформенный токен",
            type="password",
            help="Тот же bearer-токен, который платформа выдаёт для GET /api/v1/whoami.",
        )
        submitted = st.form_submit_button("Войти", type="primary")

    if not submitted:
        st.stop()

    token = token_input.strip()
    if not token:
        st.error("Введите токен.")
        st.stop()

    try:
        principal = whoami(token)
    except PlatformUnavailable:
        # Fail closed, same reasoning as `http_auth.routing.authenticate`: an
        # outage of the identity authority must not degrade into an
        # unauthenticated console, so an operator simply cannot sign in until
        # it recovers rather than being let through anonymously.
        st.error("Служба идентификации недоступна. Повторите попытку позже.")
        st.stop()

    if principal is None:
        st.error("Неверный или недействующий токен.")
        st.stop()

    st.session_state[_PRINCIPAL_KEY] = principal
    st.session_state[_TOKEN_KEY] = token
    st.rerun()


def require_identity(
    *,
    page_title: str,
    page_icon: str,
    layout: Literal["centered", "wide"] = "wide",
    sidebar_state: Literal["auto", "expanded", "collapsed", "locked"] = "expanded",
) -> Principal | None:
    """Block the entire console behind a signed-in platform identity.

    Calls ``st.set_page_config`` itself (Streamlit requires it be the first
    Streamlit command of a run, if called at all) so both the login screen
    and the authenticated app share one page-config call regardless of which
    branch a given rerun takes; ``shell.render_shell`` no longer calls it.

    Returns the session's cached :class:`Principal` once signed in. Does not
    itself authorize anything — see :func:`require_console_operation`.

    Outside a Streamlit script run this does nothing at all and returns
    ``None``. There is no browser session to gate there and no one to render
    a login form to, and — decisively — emitting one anyway is not a harmless
    no-op: the widgets land on whatever ``DeltaGenerator`` the importing
    thread happens to hold, leaving an open ``st.form`` behind that the next
    real script run in that process inherits (it surfaces as
    ``StreamlitInvalidFormCallbackError`` on an unrelated widget). Refusing is
    also unnecessary: nothing privileged happens on an import, and the one
    thing that is privileged — :func:`require_console_operation` — raises
    :class:`ConsoleGateUnavailable` there rather than returning.
    """
    if not _in_script_run():
        return None

    st.set_page_config(
        page_title=page_title,
        page_icon=page_icon,
        layout=layout,
        initial_sidebar_state=sidebar_state,
    )

    principal = _authenticated_principal()
    if principal is not None:
        return principal

    _render_login(page_title=page_title, page_icon=page_icon)


def require_console_operation(operation: str) -> Principal:
    """Re-verify live and authorize one privileged console action.

    Must be called immediately before the action it guards, never cached
    across a rerun — see the module docstring for why this differs from
    :func:`require_identity`'s session-scoped cache.

    Raises :class:`ConsoleGateUnavailable` outside a Streamlit script run,
    where every refusal primitive below (``st.error`` + ``st.stop()``) is a
    no-op and execution would simply continue into the privileged action.
    """
    if not _in_script_run():
        raise ConsoleGateUnavailable(
            f"{operation!r} was reached outside a Streamlit script run, where "
            "the console gate cannot refuse it."
        )

    token = st.session_state.get(_TOKEN_KEY)
    if not isinstance(token, str) or not token:
        _sign_out(message="Сессия не аутентифицирована. Войдите заново.")

    try:
        principal = whoami(token)
    except PlatformUnavailable:
        st.error("Служба идентификации недоступна — действие отклонено.")
        st.stop()

    if principal is None:
        _sign_out(message="Токен больше не действует. Войдите заново.")

    if not authz.is_console_permitted(principal.principal_id, operation):
        st.error("Недостаточно прав для этого действия.")
        st.stop()

    # The re-verified principal may not be the one cached at login (a
    # revoked-then-reissued credential, or the platform correcting a stale
    # tenant_id) — always hand the caller the fresh one, never the cached one.
    st.session_state[_PRINCIPAL_KEY] = principal
    return principal
