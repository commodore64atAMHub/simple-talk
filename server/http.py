#!/usr/bin/env python3
"""Entry point for the simple-talk server over HTTP.

Same mesh simulation as ``server/main.py`` (topology, BFS routing,
hop-by-hop relay animation, wire snipping), but spoken over a handful of
JSON endpoints instead of a raw TCP socket -- handy when a socket, a
firewall rule, or a proxy is more trouble than a URL is.

    python3 server/http.py                  # then, in other terminals:
    python3 client/main.py --url http://127.0.0.1:8080 --nick alice
"""

import os
import sys

# Two hazards come with calling this file http.py:
#
# 1. `server/` sits at the FRONT of sys.path while it runs, so the very
#    name `http` -- which the standard library's http.server lives
#    under -- would resolve back to this module and blow up in a
#    circular import.  Move our own directory to the back: the stdlib
#    wins for `http`, and `stserver` still resolves (it lives down here).
# 2. If some other script puts `server/` first on sys.path and then
#    imports stdlib `http`, it gets *this* module.  Pretend to be the
#    real package so at least `import http.server` keeps working.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [
    p for p in sys.path if os.path.realpath(p or os.getcwd()) != os.path.realpath(_HERE)
]
sys.path.append(_HERE)

if __name__ == "http":
    # Imported as the stdlib `http` package: either by a script that put
    # `server/` first on sys.path, or -- inside a Nuitka onefile build --
    # by the compiled module table, which answers to `http` regardless of
    # sys.path because this file is named http.py.  Either way, `import
    # http.server` (what stserver.httpserv needs) must keep working, so
    # load the real stdlib package over us and bow out.
    import importlib.machinery as _ilm
    import importlib.util as _ilu

    def _roots():
        """Candidate directories that may hold the real http package."""
        _skip = os.path.realpath(_HERE)
        _ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
        _more = []
        try:
            import sysconfig as _sc

            _p = _sc.get_paths()
            _more = [_p.get("stdlib"), _p.get("platstdlib"), _sc.get_config_var("LIBDEST")]
        except Exception:
            pass  # sysconfig is unreliable in frozen builds; guess below
        _guesses = [
            getattr(sys, "_stdlib_dir", None),
            os.path.dirname(os.__file__),
            os.path.join(sys.prefix or "", "lib", _ver),
            f"/usr/lib/{_ver}",
            f"/usr/local/lib/{_ver}",
        ]
        _out, _seen = [], set()
        for _r in (
            [p for p in sys.path if os.path.realpath(p or os.getcwd()) != _skip]
            + _more
            + _guesses
        ):
            if _r and _r not in _seen:
                _seen.add(_r)
                _out.append(_r)
        return _out

    _searched = _roots()
    _spec = _ilm.PathFinder.find_spec("http", _searched)
    if not (_spec and _spec.loader and _spec.submodule_search_locations):
        # Not on sys.path as a package (frozen builds trim sys.path):
        # look for <root>/http/__init__.py among the candidates instead.
        _spec = None
        for _root in _searched:
            _init = os.path.join(_root, "http", "__init__.py")
            if os.path.isfile(_init):
                _spec = _ilu.spec_from_file_location(
                    "http",
                    _init,
                    submodule_search_locations=[os.path.dirname(_init)],
                )
                break
    if _spec is None:
        raise ModuleNotFoundError(
            "server/http.py stands in for the stdlib `http` package but the "
            "real one could not be located; searched: " + ", ".join(_searched)
        )
    _real = _ilu.module_from_spec(_spec)
    sys.modules["http"] = _real
    _spec.loader.exec_module(_real)

if __name__ == "__main__":
    from stserver.httpserv import main

    sys.exit(main())
