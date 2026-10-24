from __future__ import annotations
import ast
import hashlib
import importlib.metadata
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

from ._version import __version__ as VERSION

VERIFIED="VERIFIED"; LOCAL_ONLY="LOCAL_ONLY"; UNVERIFIED="UNVERIFIED"; ERROR="ERROR"
def classify_status(*,live_provider_call:bool,request_id:str="",receipt_id:str="",error:bool=False,stale:bool=False)->str:
 if error:return ERROR
 if stale:return UNVERIFIED
 if not live_provider_call:return LOCAL_ONLY
 return VERIFIED if str(request_id or "").strip() or str(receipt_id or "").strip() else UNVERIFIED
def execution_provenance(*,live_provider_call:bool,transport:str="openrouter-decisions",request_id:str="",receipt_id:str="",error:bool=False,stale:bool=False)->dict[str,Any]:
 return {"engine":"hermes-nerve","version":VERSION,"transport":transport,"live_provider_call":bool(live_provider_call),"provenance_status":classify_status(live_provider_call=live_provider_call,request_id=request_id,receipt_id=receipt_id,error=error,stale=stale)}
def result_provenance(*,live_provider_call:bool,request_id:str="",receipt_id:str="",provider:str="",transport:str="openrouter-decisions",model:str="",contract:str="",created_at:str="",subject_sha256:str="",result_sha256:str="",error:bool=False,stale:bool=False)->dict[str,Any]:
 return {"verified_by":"hermes-nerve","provenance_status":classify_status(live_provider_call=live_provider_call,request_id=request_id,receipt_id=receipt_id,error=error,stale=stale),"request_id":str(request_id or ""),"receipt_id":str(receipt_id or ""),"provider":str(provider or ""),"transport":str(transport or ""),"model":str(model or ""),"contract":str(contract or ""),"created_at":str(created_at or ""),"subject_sha256":str(subject_sha256 or ""),"result_sha256":str(result_sha256 or ""),"stale":bool(stale)}
def attach(payload:dict[str,Any],*,live_provider_call:bool,transport:str="openrouter-decisions")->dict[str,Any]:
 out=dict(payload);out.setdefault("execution",execution_provenance(live_provider_call=live_provider_call,transport=transport));out.setdefault("provenance_status",out["execution"].get("provenance_status",UNVERIFIED));return out


def _identity_root() -> Path:
    """Package directory, whether installed in a plugin tree or wheel site-packages."""
    return Path(__file__).resolve().parent


def _all_code_objects(code: types.CodeType, seen: set[int] | None = None) -> list[types.CodeType]:
    if type(code) is not types.CodeType or type(code.co_consts) is not tuple:
        raise TypeError("Unverifiable code object type")
    seen = seen if seen is not None else set()
    if id(code) in seen:
        return []
    seen.add(id(code))
    result = [code]
    for value in code.co_consts:
        if type(value) is types.CodeType:
            result.extend(_all_code_objects(value, seen))
    return result


def _code_objects(value: Any, seen: set[int] | None = None, source_path: str | None = None) -> list[types.CodeType]:
    seen = seen if seen is not None else set()
    ident = id(value)
    if ident in seen:
        return []
    seen.add(ident)
    kind = type(value)
    if kind is types.FunctionType:
        code = value.__code__
        if type(code.co_filename) is not str:
            raise TypeError("Unverifiable code filename type")
        if source_path and str(Path(code.co_filename).resolve()) != source_path:
            return []
        return _all_code_objects(code)
    if issubclass(kind, (staticmethod, classmethod)):
        # Use the builtin descriptor directly, not a subclass's attribute hook.
        descriptor = staticmethod if issubclass(kind, staticmethod) else classmethod
        wrapped = descriptor.__dict__["__func__"].__get__(value)
        return _code_objects(wrapped, seen, source_path)
    if issubclass(kind, type):
        result: list[types.CodeType] = []
        # vars(cls) invokes a custom metaclass's __getattribute__. The builtin
        # dictionary descriptor reads the actual class namespace without hooks.
        namespace = type.__dict__["__dict__"].__get__(value)
        for member in namespace.values():
            result.extend(_code_objects(member, seen, source_path))
        return result
    return []


def _normalized_code(code: types.CodeType) -> tuple[Any, ...]:
    if type(code) is not types.CodeType:
        raise TypeError("Unverifiable code object type")

    def normalize(value: Any) -> Any:
        kind = type(value)
        if kind is types.CodeType:
            return _normalized_code(value)
        if kind is tuple:
            return tuple(normalize(item) for item in value)
        if kind is frozenset:
            # Normalize/validate every leaf before repr is used as a sort key.
            # Repr on the resulting exact builtin-only tree cannot call hooks.
            return tuple(sorted((normalize(item) for item in value), key=repr))
        if kind is slice:
            if all(_immutable_identity_constant(part) for part in (value.start, value.stop, value.step)):
                return value
            raise TypeError("Unverifiable slice constant component")
        if value is None or value is Ellipsis or any(kind is t for t in (str, bytes, bool, int, float, complex)):
            return value
        raise TypeError("Unverifiable code constant type")

    return (
        normalize(code.co_argcount), normalize(code.co_posonlyargcount), normalize(code.co_kwonlyargcount), normalize(code.co_nlocals),
        normalize(code.co_stacksize), normalize(code.co_flags), normalize(code.co_code), normalize(code.co_consts),
        normalize(code.co_names), normalize(code.co_varnames), normalize(code.co_freevars), normalize(code.co_cellvars),
    )


def _canonical_code_value(value: Any) -> Any:
    # Exact types, not isinstance: a recovered constant can be a subclass with
    # custom iteration/hex/repr methods. Never call those methods while observing.
    kind = type(value)
    if kind is bytes:
        return {"bytes": value.hex()}
    if kind is str:
        return {"str": value}
    if kind is tuple:
        return {"tuple": [_canonical_code_value(item) for item in value]}
    if kind is frozenset:
        items = [_canonical_code_value(item) for item in value]
        return {"frozenset": sorted(items, key=lambda item: json.dumps(item, sort_keys=True))}
    if value is None:
        return {"none": True}
    if kind is bool:
        return {"bool": value}
    if kind is int:
        return {"int": value}
    if kind is float:
        return {"float": value.hex()}
    if kind is complex:
        return {"complex": [value.real.hex(), value.imag.hex()]}
    if kind is slice:
        if all(_immutable_identity_constant(part) for part in (value.start, value.stop, value.step)):
            return {"type": "slice", "repr": repr(value)}
        raise TypeError("Unverifiable slice constant component")
    if value is Ellipsis:
        return {"type": "ellipsis", "repr": "Ellipsis"}
    raise TypeError("Unverifiable identity constant type")


def _code_digest(codes: list[types.CodeType]) -> str:
    for code in codes:
        if type(code) is not types.CodeType:
            raise TypeError("Unverifiable code object type")
        if type(code.co_name) is not str or type(getattr(code, "co_qualname", code.co_name)) is not str:
            raise TypeError("Unverifiable code name type")
    pairs = sorted(
        ((getattr(code, "co_qualname", code.co_name), _canonical_code_value(_normalized_code(code))) for code in codes),
        key=lambda item: (item[0], json.dumps(item[1], sort_keys=True, separators=(",", ":"))),
    )
    payload = json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _immutable_identity_constant(value: Any) -> bool:
    kind = type(value)
    if value is None or any(kind is t for t in (str, bytes, bool, int, float, complex)):
        return True
    if kind is tuple or kind is frozenset:
        return all(_immutable_identity_constant(item) for item in value)
    return False


def _source_identity_constants(source: bytes) -> dict[str, Any]:
    """Read literal all-caps module constants without executing plugin source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    result: dict[str, Any] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            value_node, targets = statement.value, statement.targets
        elif isinstance(statement, ast.AnnAssign):
            value_node, targets = statement.value, [statement.target]
        else:
            continue
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", target.id):
                if _immutable_identity_constant(value):
                    result[target.id] = value
    return result


def _module_identity_digest(codes: list[types.CodeType], constants: dict[str, Any]) -> str:
    canonical_constants = [
        (name, _canonical_code_value(value)) for name, value in sorted(constants.items())
    ]
    payload = json.dumps(
        {"code": _code_digest(codes), "constants": canonical_constants},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _module_code_fingerprints(module: types.ModuleType) -> tuple[str, str]:
    """Hash live/source code, or explicitly report an unverifiable observer input."""
    try:
        return _module_code_fingerprints_checked(module)
    except (TypeError, ValueError, RecursionError):
        # No repr/str of the exception or rejected value: those are plugin data.
        # Empty disk identity forces code_matches_disk=False, never a false match.
        return hashlib.sha256(b"").hexdigest(), ""


def _module_code_fingerprints_checked(module: types.ModuleType) -> tuple[str, str]:
    """Hash live module-owned code and same-interpreter code compiled from its current source."""
    if type(module) is not types.ModuleType:
        raise TypeError("Unverifiable module type")
    raw_path = vars(module).get("__file__")
    if type(raw_path) is not str:
        return hashlib.sha256(b"").hexdigest(), ""
    path = Path(raw_path).resolve()
    live_codes: list[types.CodeType] = []
    seen_values: set[int] = set()
    for value in vars(module).values():
        live_codes.extend(_code_objects(value, seen_values, str(path)))
    live_hash = _code_digest(live_codes)
    try:
        source = path.read_bytes()
        compiled = compile(source, str(path), "exec", optimize=sys.flags.optimize, dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return hashlib.sha256(b"").hexdigest(), ""
    live_names = {getattr(code, "co_qualname", code.co_name) for code in live_codes}
    disk_codes = [code for code in _all_code_objects(compiled)
                  if code is not compiled and getattr(code, "co_qualname", code.co_name) in live_names]
    source_constants = _source_identity_constants(source)
    live_constants = {
        name: vars(module)[name] for name in source_constants
        if name in vars(module) and _immutable_identity_constant(vars(module)[name])
    }
    return (
        _module_identity_digest(live_codes, live_constants),
        _module_identity_digest(disk_codes, source_constants),
    )



def _manifest_version(root: Path) -> tuple[str | None, str | None]:
    candidates = (
        root / "plugin.yaml",
        root.parent / "plugin.yaml",
        root / "share" / "hermes-nerve" / "plugin.yaml",
        root.parent / "share" / "hermes-nerve" / "plugin.yaml",
        Path(sys.prefix) / "share" / "hermes-nerve" / "plugin.yaml",
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"(?m)^version:\s*[\"']?([^\"'\s]+)", text)
        if match:
            return match.group(1), str(path.resolve())
    return None, None


def runtime_identity() -> dict[str, Any]:
    """Read-only report of loaded package modules and install metadata; no config or user data is read."""
    root = _identity_root().resolve()
    loaded: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_paths: set[str] = set()
    module_prefix = str(__package__ or "hermes_nerve")
    for name, module in sorted(sys.modules.items()):
        if (name != module_prefix and not name.startswith(module_prefix + ".")) or module is None:
            continue
        if type(module) is not types.ModuleType:
            issues.append("loaded-code-unverifiable")
            continue
        raw_path = vars(module).get("__file__")
        if type(raw_path) is not str:
            issues.append("loaded-module-source-unavailable")
            continue
        path = Path(raw_path).resolve()
        key = str(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            source_sha256 = ""
        live_code_sha256, disk_code_sha256 = _module_code_fingerprints(module)
        code_matches_disk = bool(disk_code_sha256) and live_code_sha256 == disk_code_sha256
        if not source_sha256:
            issues.append("loaded-module-source-unavailable")
        if not disk_code_sha256:
            issues.append("loaded-code-unverifiable")
        elif not code_matches_disk:
            issues.append("loaded-code-drift")
        loaded.append({
            "module": name, "path": key, "source_sha256": source_sha256,
            "code_sha256": live_code_sha256, "disk_code_sha256": disk_code_sha256,
            "code_matches_disk": code_matches_disk,
        })

    manifest_version, manifest_path = _manifest_version(root)
    try:
        distribution = importlib.metadata.distribution("hermes-nerve")
        metadata_root = Path(str(distribution.locate_file(""))).resolve()
        package_version = distribution.version if metadata_root == root.parent.resolve() else None
        package_metadata_path = str(metadata_root)
    except importlib.metadata.PackageNotFoundError:
        package_version = None
        package_metadata_path = None
    module_roots = [str(root.parent.resolve())]
    for item in loaded:
        loaded_path = Path(item["path"])
        if not (loaded_path.is_relative_to(root) or loaded_path == root.parent / "__init__.py"):
            issues.append("module-path-outside-package")
            break
    tree = hashlib.sha256()
    identity_files = [
        path for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and not (path.name.endswith(".bak") or ".bak." in path.name)
    ] if root.exists() else []
    for path in sorted(set(identity_files)):
        try:
            try:
                rel = path.relative_to(root).as_posix().encode("utf-8")
            except ValueError:
                rel = ("plugin-root/" + path.name).encode("utf-8")
            data = path.read_bytes()
        except OSError:
            continue
        tree.update(len(rel).to_bytes(4, "big")); tree.update(rel); tree.update(hashlib.sha256(data).digest())
    if manifest_version is not None and manifest_version != VERSION:
        issues.append("manifest-version-mismatch")
    if package_version is not None and package_version != VERSION:
        issues.append("package-version-mismatch")
    if not loaded:
        issues.append("no-loaded-package-modules")
    loaded_code_tree = hashlib.sha256()
    for item in sorted(loaded, key=lambda row: row["path"]):
        path = Path(item["path"])
        try:
            rel = path.relative_to(root.parent).as_posix()
        except ValueError:
            rel = path.name
        loaded_code_tree.update(rel.encode("utf-8"))
        loaded_code_tree.update(item["code_sha256"].encode("ascii"))
    return {
        "version": VERSION,
        "build_id": f"{VERSION}+{tree.hexdigest()[:16]}",
        "loaded_code_tree_sha256": loaded_code_tree.hexdigest(),
        "status": "DRIFT" if issues else ("MATCH" if manifest_version == VERSION and loaded else "UNKNOWN"),
        "issues": issues,
        "plugin_root": module_roots[0] if len(module_roots) == 1 else str(root.parent),
        "package_root": str(root),
        "module_roots": module_roots,
        "manifest_path": manifest_path,
        "manifest_version": manifest_version,
        "package_version": package_version,
        "package_metadata_path": package_metadata_path,
        "source_tree_sha256": tree.hexdigest(),
        "loaded_module_count": len(loaded),
        "loaded_modules": loaded,
    }
