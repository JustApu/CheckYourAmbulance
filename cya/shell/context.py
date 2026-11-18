from cya.shell import navigation


def shell(request):
    """Template context for the shell: the signed-in user and their navigation."""
    user = getattr(request, "cya_user", None)
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else None
    # A section also owns its sub-pages, named "<section>-<page>".
    current = next(
        (
            s.key
            for s in navigation.SECTIONS
            if name and (name == s.url_name or name.startswith(s.url_name + "-"))
        ),
        None,
    )
    nav = navigation.app_navigation(user, request.path) if user is not None else None
    more = nav["more"] if nav else ()
    return {
        "cya_user": user,
        "cya_menu": navigation.menu(user, current),
        "cya_nav": nav,
        # Empty lists point people who can set things up to Administration (issue #194).
        "cya_admin_url": next((m["url"] for m in more if m["key"] == "administration"), None),
    }
