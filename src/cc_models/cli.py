"""cc-models CLI: list / refresh / apply / backups / restore / TUI launcher.

Secrets policy: token *values* are resolved in memory for catalog requests and
never printed, logged or persisted. Output only names the token's source
(environment variable name or "settings token (same endpoint only)").

Apply safety: without ``--yes`` and without ``--dry-run`` a destructive apply
is refused (exit 2). Backups/restore follow the core's drift-safe writer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from importlib import metadata
from pathlib import Path

from .domain import CoreError, DriftError, EndpointError, parse_strict_json
from .session import (
    DEFAULT_SETTINGS_PATH,
    DEFAULT_STATE_DIR,
    SessionContext,
    SessionError,
    build_demo_context,
    load_session,
    resolve_api_key,
    resolve_endpoint,
)
from .settings import apply_selection, preview_apply, restore_backup
from .store import record_fetch

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, SessionError):
        return str(exc)
    return f"错误: {exc}"


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH), help="设置文件路径")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="本地状态目录")
    parser.add_argument(
        "--base-url",
        default=None,
        help="本次运行的端点覆盖（仅当次生效，不写入本地状态；TUI 配置中才持久化）",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="允许远程明文 HTTP 端点（凭据将经明文传输，需显式确认）",
    )
    parser.add_argument("--demo", action="store_true", help="使用确定性临时演示数据，不访问网络")
    parser.add_argument(
        "--version",
        action="version",
        version=metadata.version("cc-models"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-models", description="CPA 模型目录选择器（modelPicker 配置管理，不代理请求）"
    )
    _add_global_args(parser)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="列出目录与当前选择（离线）")
    sub.add_parser("refresh", help="从上游刷新模型目录（GET /v1/models）")
    apply_p = sub.add_parser("apply", help="预览或应用当前选择")
    apply_p.add_argument("--dry-run", action="store_true", help="仅预览受管键变更")
    apply_p.add_argument("--yes", action="store_true", help="确认应用（写入设置文件）")
    sub.add_parser("backups", help="列出该设置文件的备份")
    restore_p = sub.add_parser("restore", help="从备份恢复设置文件")
    restore_p.add_argument("--backup", required=True, help="备份文件路径")
    restore_p.add_argument("--yes", action="store_true", help="确认恢复（整文件替换）")
    sub.add_parser("tui", help="启动文本界面（默认）")
    return parser


def _load_context(args: argparse.Namespace) -> SessionContext:
    if args.demo:
        return build_demo_context()
    return load_session(args.settings, args.state_dir)


def _resolution(ctx: SessionContext, args: argparse.Namespace):
    from .domain import normalize_endpoint

    resolution = resolve_endpoint(ctx, allow_insecure_http=args.allow_insecure_http)
    allow_insecure = resolution.allow_insecure_http
    if args.base_url:
        # Per-run override only; validated here (credentials/remote plain HTTP
        # without the opt-in are refused) and never written to local state.
        try:
            endpoint = normalize_endpoint(args.base_url, allow_insecure_http=allow_insecure)
        except (EndpointError, ValueError) as exc:
            raise EndpointError(
                "invalid endpoint: credentials, query or fragment are not allowed; "
                "use a valid HTTPS or opted-in HTTP URL"
            ) from exc
        return type(resolution)(
            raw=endpoint,
            source="命令行 --base-url",
            allow_insecure_http=allow_insecure,
            token_env=resolution.token_env,
        )
    if args.allow_insecure_http:
        return type(resolution)(
            raw=resolution.raw,
            source=resolution.source,
            allow_insecure_http=True,
            token_env=resolution.token_env,
        )
    return resolution


def cmd_list(ctx: SessionContext, args: argparse.Namespace) -> int:
    resolution = _resolution(ctx, args)
    _key, key_source = resolve_api_key(ctx, resolution)
    try:
        catalog = ctx.catalog_for_current_endpoint(resolution.raw, resolution.allow_insecure_http)
    except CoreError as exc:
        catalog = None
        print(f"（缓存不可用: {exc}）")
    print(f"设置文件: {ctx.settings_path}")
    print(f"状态目录: {ctx.state_dir}")
    if ctx.demo:
        print("模式: --demo（临时演示数据，不访问网络）")
    print(f"端点: {resolution.raw}（来源: {resolution.source}）")
    if resolution.allow_insecure_http:
        print("警告: 已启用远程明文 HTTP opt-in，令牌将经明文传输。")
    print(f"令牌来源: {key_source}")
    if catalog is None:
        print("目录: 无缓存（运行 refresh 获取）")
    else:
        print(
            f"目录: 缓存 {catalog.endpoint} @ {catalog.fetched_at}（{len(catalog.model_ids)} 个模型）"
        )
    print(
        f"replaceBuiltInOptions 覆盖: {ctx.selection.replace_built_in if ctx.selection.replace_built_in is not None else '（跟随设置文件）'}"
    )
    if ctx.imported_from_settings:
        print("选择: 启动时从设置文件 modelPicker 导入（本地状态为空）")
    else:
        print("选择: 来自本地状态")
    selected_ids = ctx.selection.ids
    default_id = (
        ctx.selection.default_edit.value if ctx.selection.default_edit.kind == "set" else None
    )
    if selected_ids:
        print("已选（顺序）:")
        for i, option in enumerate(ctx.selection.options, start=1):
            markers = []
            if option.model == default_id:
                markers.append("默认")
            if catalog is not None and not catalog.contains(option.model):
                markers.append("缺失")
            suffix = f"  [{'、'.join(markers)}]" if markers else ""
            desc = f" — {option.description}" if option.description else ""
            print(f"  {i}. {option.model} — {option.label}{desc}{suffix}")
    else:
        print("已选: （空）")
    if catalog is not None:
        rest = [m for m in catalog.model_ids if m not in selected_ids]
        if rest:
            print("目录中未选:")
            for model_id in rest:
                print(f"  - {model_id}")
    return EXIT_OK


def cmd_refresh(ctx: SessionContext, args: argparse.Namespace) -> int:
    resolution = _resolution(ctx, args)
    if ctx.demo and ctx.catalog is not None:
        # Demo refresh rebuilds the deterministic fixture catalog; no HTTP ever.
        catalog = ctx.catalog
        key_source = "演示数据（不访问网络）"
    else:
        api_key, key_source = resolve_api_key(ctx, resolution)
        from .catalog import fetch_catalog

        try:
            catalog = fetch_catalog(
                resolution.raw,
                api_key=api_key,
                allow_insecure_http=resolution.allow_insecure_http,
            )
        except CoreError as exc:
            print(_friendly(exc))
            print(
                f"目录获取失败（令牌来源: {key_source}）；本地选择保持不变。请检查端点/网络后重试。"
            )
            return EXIT_ERROR
    record_fetch(ctx.state_dir, catalog, ctx.selection)
    ctx.catalog = catalog
    print(f"端点: {catalog.endpoint}")
    print(f"获取时间: {catalog.fetched_at}")
    print(f"模型 ({len(catalog.model_ids)}):")
    for model_id in catalog.model_ids:
        marker = " ✓" if model_id in ctx.selection.ids else ""
        print(f"  - {model_id}{marker}")
    if catalog.duplicates:
        print(f"去重: {catalog.duplicates}")
    return EXIT_OK


def _render_changes(changes: dict) -> str:
    return json.dumps(changes, ensure_ascii=False, indent=2)


def cmd_apply(ctx: SessionContext, args: argparse.Namespace) -> int:
    resolution = _resolution(ctx, args)
    catalog = ctx.catalog_for_current_endpoint(resolution.raw, resolution.allow_insecure_http)
    try:
        preview = preview_apply(ctx.snapshot, ctx.selection, catalog)
    except CoreError as exc:
        print(_friendly(exc))
        return EXIT_ERROR
    if args.dry_run:
        print("=== 预览（仅受管键，不含其他设置或凭据） ===")
        print(_render_changes(preview.managed_changes))
        for warning in preview.warnings:
            print(f"警告: {warning}")
        print(f"将写入: {ctx.settings_path}" if preview.would_write else "无变更（no-op）")
        return EXIT_OK
    if not args.yes:
        print("拒绝：应用会修改设置文件。请先 `apply --dry-run` 预览，确认后用 `apply --yes`。")
        return EXIT_REFUSED
    try:
        result = apply_selection(ctx.snapshot, ctx.selection, ctx.state_dir, catalog)
    except CoreError as exc:
        print(_friendly(exc))
        if isinstance(exc, DriftError):
            print("设置文件已被外部修改。请重新运行 cc-models 以加载最新快照。")
        return EXIT_ERROR
    if result.no_op:
        print("无变更（no-op），未创建备份。")
    else:
        print(f"已应用: {ctx.settings_path}")
        assert result.backup_path is not None
        print(f"备份: {result.backup_path}")
    for warning in result.warnings:
        print(f"警告: {warning}")
    return EXIT_OK


def cmd_backups(ctx: SessionContext, args: argparse.Namespace) -> int:
    target = Path(ctx.settings_path)
    pattern = f"{target.name}.cc-models-backup-*"
    backups = sorted(target.parent.glob(pattern))
    if not backups:
        print(f"没有备份（模式: {pattern}）")
        return EXIT_OK
    for backup in backups:
        stat = backup.stat()
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        print(f"{backup}  {stat.st_size} 字节  {mtime}")
    return EXIT_OK


def cmd_restore(ctx: SessionContext, args: argparse.Namespace) -> int:
    backup_path = Path(args.backup)
    if not backup_path.exists():
        print(f"错误: 备份文件不存在: {backup_path}")
        return EXIT_ERROR
    # preview the managed diff before confirmation
    try:
        backup_data = parse_strict_json(backup_path.read_text(encoding="utf-8"))
        if not isinstance(backup_data, dict):
            raise CoreError("backup JSON root must be an object")
        changes = {}
        for key in ("modelPicker", "model"):
            if ctx.snapshot.data.get(key) != backup_data.get(key):
                changes[key] = {"before": ctx.snapshot.data.get(key), "after": backup_data.get(key)}
        from .settings import redact_managed_changes

        print("=== 恢复预览（仅受管键；恢复会整文件替换并覆盖无关设置） ===")
        print(_render_changes(redact_managed_changes(changes)) if changes else "受管键无差异")
    except (OSError, ValueError, CoreError) as exc:
        print(f"错误: 备份不可读或不是有效 JSON: {exc}")
        return EXIT_ERROR
    if not args.yes:
        print("拒绝：恢复会整文件替换设置。确认无误后加 --yes。")
        return EXIT_REFUSED
    try:
        result = restore_backup(ctx.snapshot, backup_path, ctx.state_dir, confirm=True)
    except CoreError as exc:
        print(_friendly(exc))
        return EXIT_ERROR
    if result.no_op:
        print("备份与当前内容一致（no-op）。")
    else:
        print(f"已恢复: {ctx.settings_path}")
        assert result.backup_of_current is not None
        print(f"恢复前备份: {result.backup_of_current}")
    for warning in result.warnings:
        print(f"警告: {warning}")
    return EXIT_OK


def cmd_tui(ctx: SessionContext, args: argparse.Namespace) -> int:
    from .app import run_app

    run_app(ctx)
    return EXIT_OK


COMMANDS = {
    "list": cmd_list,
    "refresh": cmd_refresh,
    "apply": cmd_apply,
    "backups": cmd_backups,
    "restore": cmd_restore,
    "tui": cmd_tui,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "tui"
    try:
        ctx = _load_context(args)
    except (CoreError, EndpointError, OSError) as exc:
        print(_friendly(exc))
        return EXIT_ERROR
    try:
        return COMMANDS[command](ctx, args)
    except CoreError as exc:
        print(_friendly(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已取消。")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
