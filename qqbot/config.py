"""配置加载：YAML + .env + 环境变量展开 + 严格校验。

设计原则：配置写错要立刻报错并指出是哪个字段，而不是跑起来才莫名其妙。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


class ConfigError(Exception):
    """配置有问题。"""


# ${VAR} / ${VAR:-默认值}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# --------------------------------------------------------------------------- #
# 基础解析工具
# --------------------------------------------------------------------------- #
def _path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _ensure_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where} 里存在未知配置项：{', '.join(unknown)}（检查拼写？）")


def as_str(value: Any, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise ConfigError(f"{where} 应该是字符串，实际是 {type(value).__name__}")


def as_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where} 应该是整数，实际是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    raise ConfigError(f"{where} 应该是整数，实际是 {value!r}")


def as_float(value: Any, where: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{where} 应该是数字，实际是布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    raise ConfigError(f"{where} 应该是数字，实际是 {value!r}")


def as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0", ""):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ConfigError(f"{where} 应该是 true/false，实际是 {value!r}")


def _as_list(value: Any, where: str, item: Callable[[Any, str], Any]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):
        # 允许 "123,456" 这种写法
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [item(p, where) for p in parts]
    raise ConfigError(f"{where} 应该是列表，实际是 {value!r}")


def as_int_list(value: Any, where: str) -> list[int]:
    return _as_list(value, where, as_int)


def as_str_list(value: Any, where: str) -> list[str]:
    return _as_list(value, where, as_str)


# --------------------------------------------------------------------------- #
# 各配置段
# --------------------------------------------------------------------------- #
@dataclass
class TriggerConfig:
    """什么情况下该回话。"""

    private: bool = True
    groups: list[int] = field(default_factory=list)
    exclude_groups: list[int] = field(default_factory=list)
    require_mention: bool = True
    respond_to_at_all: bool = False
    respond_to_images: bool = False
    wake_words: list[str] = field(default_factory=list)
    prefix: str = ""

    @classmethod
    def from_dict(cls, data: Any, where: str = "bot.trigger") -> "TriggerConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        allowed = {
            "private", "groups", "exclude_groups", "require_mention",
            "respond_to_at_all", "respond_to_images", "wake_words", "prefix",
        }
        _ensure_keys(data, allowed, where)
        return cls(
            private=as_bool(data.get("private", True), _path(where, "private")),
            groups=as_int_list(data.get("groups"), _path(where, "groups")),
            exclude_groups=as_int_list(data.get("exclude_groups"), _path(where, "exclude_groups")),
            require_mention=as_bool(data.get("require_mention", True), _path(where, "require_mention")),
            respond_to_at_all=as_bool(data.get("respond_to_at_all", False), _path(where, "respond_to_at_all")),
            respond_to_images=as_bool(
                data.get("respond_to_images", False), _path(where, "respond_to_images")
            ),
            wake_words=as_str_list(data.get("wake_words"), _path(where, "wake_words")),
            prefix=as_str(data.get("prefix", ""), _path(where, "prefix")),
        )


@dataclass
class BotConfig:
    """机器人身份与行为。"""

    name: str = "小D"
    command_prefix: str = "/"
    admins: list[int] = field(default_factory=list)
    reply_with_at: bool = True
    reply_with_quote: bool = False
    max_reply_chars: int = 1200
    concurrency: int = 4
    cooldown_seconds: float = 3.0
    empty_mention_reply: str = "在的，怎么啦？"
    timezone: str = "Asia/Shanghai"
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    deny_users: list[int] = field(default_factory=list)
    allow_users: list[int] = field(default_factory=list)
    stickers_dir: str = "stickers"
    max_stickers_per_reply: int = 1

    @classmethod
    def from_dict(cls, data: Any, where: str = "bot") -> "BotConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        allowed = {
            "name", "command_prefix", "admins", "reply_with_at", "reply_with_quote",
            "max_reply_chars", "concurrency", "cooldown_seconds", "empty_mention_reply",
            "timezone", "trigger", "deny_users", "allow_users",
            "stickers_dir", "max_stickers_per_reply",
        }
        _ensure_keys(data, allowed, where)
        cfg = cls(
            name=as_str(data.get("name", "小D"), _path(where, "name")).strip() or "小D",
            command_prefix=as_str(data.get("command_prefix", "/"), _path(where, "command_prefix")),
            admins=as_int_list(data.get("admins"), _path(where, "admins")),
            reply_with_at=as_bool(data.get("reply_with_at", True), _path(where, "reply_with_at")),
            reply_with_quote=as_bool(data.get("reply_with_quote", False), _path(where, "reply_with_quote")),
            max_reply_chars=as_int(data.get("max_reply_chars", 1200), _path(where, "max_reply_chars")),
            concurrency=as_int(data.get("concurrency", 4), _path(where, "concurrency")),
            cooldown_seconds=as_float(data.get("cooldown_seconds", 3.0), _path(where, "cooldown_seconds")),
            empty_mention_reply=as_str(
                data.get("empty_mention_reply", "在的，怎么啦？"), _path(where, "empty_mention_reply")
            ),
            timezone=as_str(data.get("timezone", "Asia/Shanghai"), _path(where, "timezone")),
            trigger=TriggerConfig.from_dict(data.get("trigger"), _path(where, "trigger")),
            deny_users=as_int_list(data.get("deny_users"), _path(where, "deny_users")),
            allow_users=as_int_list(data.get("allow_users"), _path(where, "allow_users")),
            stickers_dir=as_str(data.get("stickers_dir", "stickers"), _path(where, "stickers_dir")),
            max_stickers_per_reply=as_int(
                data.get("max_stickers_per_reply", 1), _path(where, "max_stickers_per_reply")
            ),
        )
        if cfg.concurrency < 1:
            raise ConfigError("bot.concurrency 至少为 1")
        if cfg.max_reply_chars < 50:
            raise ConfigError("bot.max_reply_chars 太小了，至少 50")
        return cfg


@dataclass
class LLMConfig:
    """大模型接口（OpenAI 兼容）。"""

    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout: float = 60.0
    max_history_turns: int = 10
    max_history_chars: int = 4000
    history_ttl_hours: float = 24.0
    system_prompt: str = ""

    @classmethod
    def from_dict(cls, data: Any, where: str = "llm") -> "LLMConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        allowed = {
            "base_url", "api_key", "model", "temperature", "max_tokens", "timeout",
            "max_history_turns", "max_history_chars", "history_ttl_hours", "system_prompt",
        }
        _ensure_keys(data, allowed, where)
        cfg = cls(
            base_url=as_str(data.get("base_url", "https://api.deepseek.com"), _path(where, "base_url")),
            api_key=as_str(data.get("api_key", ""), _path(where, "api_key")),
            model=as_str(data.get("model", "deepseek-chat"), _path(where, "model")),
            temperature=as_float(data.get("temperature", 0.8), _path(where, "temperature")),
            max_tokens=as_int(data.get("max_tokens", 1024), _path(where, "max_tokens")),
            timeout=as_float(data.get("timeout", 60.0), _path(where, "timeout")),
            max_history_turns=as_int(data.get("max_history_turns", 10), _path(where, "max_history_turns")),
            max_history_chars=as_int(data.get("max_history_chars", 4000), _path(where, "max_history_chars")),
            history_ttl_hours=as_float(data.get("history_ttl_hours", 24.0), _path(where, "history_ttl_hours")),
            system_prompt=as_str(data.get("system_prompt", ""), _path(where, "system_prompt")),
        )
        if not cfg.api_key.strip():
            raise ConfigError(
                "llm.api_key 是空的。请复制 .env.example 为 .env，填上 DEEPSEEK_API_KEY=sk-xxx；"
                "或者直接在 config.yaml 的 llm.api_key 里写死（注意别提交到公开仓库）"
            )
        if cfg.max_history_turns < 0:
            raise ConfigError("llm.max_history_turns 不能为负数")
        return cfg


@dataclass
class MemoryConfig:
    db_path: str = "data/memory.db"
    prune_interval_minutes: float = 30.0

    @classmethod
    def from_dict(cls, data: Any, where: str = "memory") -> "MemoryConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        _ensure_keys(data, {"db_path", "prune_interval_minutes"}, where)
        return cls(
            db_path=as_str(data.get("db_path", "data/memory.db"), _path(where, "db_path")),
            prune_interval_minutes=as_float(
                data.get("prune_interval_minutes", 30.0), _path(where, "prune_interval_minutes")
            ),
        )


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = ""

    @classmethod
    def from_dict(cls, data: Any, where: str = "log") -> "LogConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        _ensure_keys(data, {"level", "file"}, where)
        cfg = cls(
            level=as_str(data.get("level", "INFO"), _path(where, "level")).upper(),
            file=as_str(data.get("file", ""), _path(where, "file")),
        )
        if cfg.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"log.level 只能是 DEBUG/INFO/WARNING/ERROR，实际是 {cfg.level}")
        return cfg


@dataclass
class VisionConfig:
    """图片/表情包识别（调用一个多模态模型当"眼睛"）。"""

    enabled: bool = False
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    model: str = "Qwen/Qwen2.5-VL-32B-Instruct"
    timeout: float = 60.0
    max_images: int = 3
    max_bytes: int = 4 * 1024 * 1024
    prompt: str = (
        "你是聊天机器人的眼睛。请用中文客观描述这张图片，供机器人在 QQ 里理解它。\n"
        "要求：\n"
        "- 表情包/梗图：说清画面内容、图上的文字、以及它想表达的情绪或梗。\n"
        "- 照片/截图：说清主体、场景、画面里的文字。\n"
        "- 不超过 80 字，不要写“这是一张图片”这种废话，不要客套。"
    )

    @classmethod
    def from_dict(cls, data: Any, where: str = "vision") -> "VisionConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        allowed = {
            "enabled", "base_url", "api_key", "model", "timeout",
            "max_images", "max_bytes", "prompt",
        }
        _ensure_keys(data, allowed, where)
        cfg = cls(
            enabled=as_bool(data.get("enabled", False), _path(where, "enabled")),
            base_url=as_str(
                data.get("base_url", "https://api.siliconflow.cn/v1"), _path(where, "base_url")
            ).strip(),
            api_key=as_str(data.get("api_key", ""), _path(where, "api_key")).strip(),
            model=as_str(
                data.get("model", "Qwen/Qwen2.5-VL-32B-Instruct"), _path(where, "model")
            ).strip(),
            timeout=as_float(data.get("timeout", 60.0), _path(where, "timeout")),
            max_images=as_int(data.get("max_images", 3), _path(where, "max_images")),
            max_bytes=as_int(data.get("max_bytes", 4 * 1024 * 1024), _path(where, "max_bytes")),
            prompt=as_str(data.get("prompt", ""), _path(where, "prompt")),
        )
        if cfg.enabled and not cfg.api_key:
            raise ConfigError(
                "vision.enabled 是 true，但 vision.api_key 是空的。\n"
                "  要么把 .env 里的 VISION_API_KEY 填上（硅基流动 / 智谱 等任一多模态模型的 Key），\n"
                "  要么把 config.yaml 里的 vision.enabled 改回 false。"
            )
        if cfg.enabled and not re.match(r"^https?://", cfg.base_url):
            raise ConfigError("vision.base_url 必须以 http:// 或 https:// 开头")
        if cfg.max_images < 1:
            raise ConfigError("vision.max_images 至少为 1")
        if not cfg.prompt.strip():
            raise ConfigError("vision.prompt 不能为空")
        return cfg


@dataclass
class QQConfig:
    """OneBot 连接参数。"""

    mode: str = "forward"
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    self_id: int = 0
    reverse_host: str = "0.0.0.0"
    reverse_port: int = 3001
    reverse_path: str = "/onebot"
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0

    @classmethod
    def from_dict(cls, data: Any, where: str = "qq") -> "QQConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"{where} 应该是一个映射（缩进块）")
        allowed = {
            "mode", "ws_url", "access_token", "self_id", "reverse_host",
            "reverse_port", "reverse_path", "reconnect_min", "reconnect_max",
        }
        _ensure_keys(data, allowed, where)
        cfg = cls(
            mode=as_str(data.get("mode", "forward"), _path(where, "mode")).strip().lower(),
            ws_url=as_str(data.get("ws_url", "ws://127.0.0.1:3001"), _path(where, "ws_url")).strip(),
            access_token=as_str(data.get("access_token", ""), _path(where, "access_token")).strip(),
            self_id=as_int(data.get("self_id", 0), _path(where, "self_id")),
            reverse_host=as_str(data.get("reverse_host", "0.0.0.0"), _path(where, "reverse_host")),
            reverse_port=as_int(data.get("reverse_port", 3001), _path(where, "reverse_port")),
            reverse_path=as_str(data.get("reverse_path", "/onebot"), _path(where, "reverse_path")),
            reconnect_min=as_float(data.get("reconnect_min", 1.0), _path(where, "reconnect_min")),
            reconnect_max=as_float(data.get("reconnect_max", 30.0), _path(where, "reconnect_max")),
        )
        if cfg.mode not in ("forward", "reverse"):
            raise ConfigError(f"qq.mode 只能是 forward 或 reverse，实际是 {cfg.mode!r}")
        if cfg.mode == "forward" and not re.match(r"^wss?://", cfg.ws_url):
            raise ConfigError("qq.ws_url 必须以 ws:// 或 wss:// 开头，例如 ws://127.0.0.1:3001")
        if not 0 < cfg.reverse_port < 65536:
            raise ConfigError("qq.reverse_port 超出范围")
        if not cfg.reverse_path.startswith("/"):
            cfg.reverse_path = "/" + cfg.reverse_path
        if cfg.reconnect_min <= 0 or cfg.reconnect_max < cfg.reconnect_min:
            raise ConfigError("qq.reconnect_min / reconnect_max 设置不合理")
        return cfg


# --------------------------------------------------------------------------- #
# 顶层
# --------------------------------------------------------------------------- #
@dataclass
class AppConfig:
    qq: QQConfig = field(default_factory=QQConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    log: LogConfig = field(default_factory=LogConfig)
    source: Path | None = None

    def resolve_paths(self) -> None:
        """把相对路径解析成 config.yaml 所在目录下的绝对路径。"""
        base = self.source.parent if self.source else Path.cwd()
        for attr in ("db_path",):
            value = getattr(self.memory, attr)
            if value:
                p = Path(value).expanduser()
                setattr(self.memory, attr, str(p if p.is_absolute() else (base / p)))
        if self.log.file:
            p = Path(self.log.file).expanduser()
            self.log.file = str(p if p.is_absolute() else (base / p))
        if self.bot.stickers_dir:
            p = Path(self.bot.stickers_dir).expanduser()
            self.bot.stickers_dir = str(p if p.is_absolute() else (base / p))

    def summary(self) -> str:
        key = self.llm.api_key
        masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("已配置" if key else "未配置")
        trig = self.bot.trigger
        if trig.groups:
            groups = "、".join(str(g) for g in trig.groups)
        else:
            groups = "全部群"
        return "\n".join(
            [
                f"  接入方式   {self.qq.mode}  "
                + (self.qq.ws_url if self.qq.mode == "forward"
                   else f"ws://{self.qq.reverse_host}:{self.qq.reverse_port}{self.qq.reverse_path}"),
                f"  机器人     {self.bot.name}（self_id={self.qq.self_id or '自动'}）",
                f"  模型       {self.llm.model} @ {self.llm.base_url}",
                f"  API Key    {masked}",
                f"  可回复群   {groups}"
                + ("" if trig.require_mention else "（所有消息都回）")
                + ("，需 @ 或唤醒词" if trig.require_mention else ""),
                f"  私聊       {'开启' if trig.private else '关闭'}",
                f"  看图       "
                + (f"开启（{self.vision.model}）" if self.vision.enabled else "关闭"),
                f"  上下文     {self.llm.max_history_turns} 轮 / {self.llm.history_ttl_hours:g} 小时过期",
                f"  数据库     {self.memory.db_path}",
            ]
        )


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            return default if default is not None else ""

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释和引号，已存在的环境变量不覆盖。"""
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def load_config(path: str | Path) -> AppConfig:
    """加载并校验配置；出错抛 ConfigError（带人话解释）。"""
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (Path.cwd() / cfg_path)
    cfg_path = cfg_path.resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"找不到配置文件：{cfg_path}")

    # .env 优先读同目录，其次当前工作目录
    load_dotenv(cfg_path.parent / ".env")
    if cfg_path.parent != Path.cwd():
        load_dotenv(Path.cwd() / ".env")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 语法错误：{exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 根节点必须是键值对（例如 qq: / bot: / llm:）")

    _ensure_keys(raw, {"qq", "bot", "llm", "vision", "memory", "log"}, "配置文件顶层")
    raw = _expand_env(raw)

    cfg = AppConfig(
        qq=QQConfig.from_dict(raw.get("qq")),
        bot=BotConfig.from_dict(raw.get("bot")),
        llm=LLMConfig.from_dict(raw.get("llm")),
        vision=VisionConfig.from_dict(raw.get("vision")),
        memory=MemoryConfig.from_dict(raw.get("memory")),
        log=LogConfig.from_dict(raw.get("log")),
        source=cfg_path,
    )
    cfg.resolve_paths()
    return cfg
