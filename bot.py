import discord
import os
import re
import asyncio
import traceback
import atexit
import sqlite3
import uuid
import aiohttp
import base64
import random
import string
import json
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse
from typing import List, Dict, Set, Optional, Any, Tuple
from dataclasses import dataclass, field

try:
    from tree_sitter_languages import get_language, get_parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

try:
    import tree_sitter_lua
    LUA_PARSER_AVAILABLE = True
except ImportError:
    LUA_PARSER_AVAILABLE = False

intents = discord.Intents.all()

# Runtime safety: prevent the same Discord message from being handled
# more than once, including when multiple local bot processes are running.
_DEDUPE_DB = os.path.join("tmp", "command_dedupe.sqlite3")
os.makedirs("tmp", exist_ok=True)

def _claim_message(message_id):
    conn = sqlite3.connect(_DEDUPE_DB, timeout=5)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_messages "
            "(message_id TEXT PRIMARY KEY, created_at REAL NOT NULL)"
        )
        try:
            conn.execute(
                "INSERT INTO processed_messages(message_id, created_at) VALUES (?, ?)",
                (str(message_id), datetime.utcnow().timestamp())
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        finally:
            # Keep the DB small.
            conn.execute(
                "DELETE FROM processed_messages WHERE created_at < ?",
                (datetime.utcnow().timestamp() - 86400,)
            )
            conn.commit()
    finally:
        conn.close()

def _safe_filename(name, fallback="upload.txt"):
    name = os.path.basename(name or "").replace("\\x00", "")
    if not name or name in {".", ".."}:
        return fallback
    return name[:180]

bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)

class DuplicateCommandEvent(commands.CommandError):
    pass

@bot.before_invoke
async def prevent_duplicate_dispatch(ctx):
    if not _claim_message(ctx.message.id):
        raise DuplicateCommandEvent()


# ==========================================================
# PRIVATE API CHAT (IN-MEMORY ONLY)
# ==========================================================
# API credentials are intentionally kept only in memory and are never written
# to disk, logs, embeds, or generated reports. Each user gets an isolated
# conversation. This supports OpenAI-compatible chat APIs; arbitrary REST APIs
# still require an endpoint/schema and are not treated as free-form chat.
_API_SESSIONS: Dict[int, Dict[str, Any]] = {}
_API_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_API_LAST_REQUEST: Dict[int, float] = {}
_API_MAX_HISTORY = 20
_API_MAX_MESSAGE_CHARS = 8000

def _api_endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip('/')
    if base.endswith('/chat/completions'):
        return base
    return base + '/chat/completions'

def _trim_history(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    # Keep the system prompt plus the most recent turns.
    if not history:
        return history
    system = [m for m in history if m.get('role') == 'system'][:1]
    rest = [m for m in history if m.get('role') != 'system'][-(_API_MAX_HISTORY - len(system)): ]
    return system + rest

async def _api_chat(user_id: int, prompt: str) -> str:
    session = _API_SESSIONS.get(user_id)
    if not session:
        return 'No API is configured. Use `/api` in this DM first.'
    prompt = prompt.strip()
    if not prompt:
        return 'Send a message after configuring the API.'
    if len(prompt) > _API_MAX_MESSAGE_CHARS:
        return f'Message is too long. Maximum: {_API_MAX_MESSAGE_CHARS} characters.'

    now = asyncio.get_running_loop().time()
    last = _API_LAST_REQUEST.get(user_id, 0.0)
    if now - last < 1.5:
        return 'Please wait a moment before sending another API request.'
    _API_LAST_REQUEST[user_id] = now

    async with _API_LOCKS[user_id]:
        history = session.setdefault('history', [{'role': 'system', 'content': session.get('system', 'You are a helpful assistant.')}])
        history.append({'role': 'user', 'content': prompt})
        session['history'] = _trim_history(history)

        payload = {
            'model': session['model'],
            'messages': session['history'],
            'temperature': session.get('temperature', 0.7),
        }
        headers = {
            'Authorization': 'Bearer ' + session['key'],
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        try:
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(_api_endpoint(session['base_url']), json=payload, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status >= 400:
                        # Never echo the key or Authorization header.
                        return f'API error {resp.status}: {raw[:1200]}'
                    try:
                        data = json.loads(raw)
                    except Exception:
                        return 'API returned a non-JSON response.'

            answer = ''
            choices = data.get('choices') or []
            if choices:
                message = choices[0].get('message') or {}
                answer = message.get('content') or ''
            if not answer:
                answer = data.get('output_text') or data.get('response') or ''
            if not isinstance(answer, str) or not answer.strip():
                return 'The API returned no text content.'

            session['history'].append({'role': 'assistant', 'content': answer})
            session['history'] = _trim_history(session['history'])
            return answer
        except asyncio.TimeoutError:
            return 'API request timed out.'
        except aiohttp.ClientError as exc:
            return 'API connection error: ' + str(exc)[:500]
        except Exception as exc:
            print('API CHAT ERROR:', repr(exc))
            return 'Unexpected API error: ' + str(exc)[:500]

async def _wait_dm(interaction: discord.Interaction, prompt: str, *, secret=False) -> Optional[discord.Message]:
    await interaction.followup.send(prompt)
    channel_id = interaction.channel_id
    user_id = interaction.user.id
    def check(m: discord.Message) -> bool:
        return m.author.id == user_id and m.channel.id == channel_id
    try:
        return await bot.wait_for('message', check=check, timeout=180)
    except asyncio.TimeoutError:
        await interaction.followup.send('Setup timed out. Run `/api` again.')
        return None

@bot.tree.command(name='api', description='Configure a private OpenAI-compatible API chat in DMs')
async def api_command(interaction: discord.Interaction):
    if interaction.guild is not None:
        await interaction.response.send_message('🔒 Use `/api` in a DM with me.', ephemeral=True)
        return

    await interaction.response.defer()
    user_id = interaction.user.id
    await interaction.followup.send(
        '**Private API setup**\n'
        'This setup stays in this DM. I will keep the API key **in memory only** and will not write it to disk or logs.\n\n'
        'First, send the API base URL (for example `https://api.openai.com/v1`).'
    )
    base_msg = await _wait_dm(interaction, 'Send the API base URL now (or `default` for OpenAI).')
    if not base_msg:
        return
    base = base_msg.content.strip()
    if base.lower() == 'default':
        base = 'https://api.openai.com/v1'
    if not re.match(r'^https://[^\s]+$', base, re.IGNORECASE):
        await interaction.followup.send('❌ Use an HTTPS API base URL.')
        return

    key_msg = await _wait_dm(interaction, 'Now send the API key. I will delete that message immediately if Discord allows it.')
    if not key_msg:
        return
    key = key_msg.content.strip()
    try:
        await key_msg.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    if len(key) < 10 or len(key) > 1000:
        await interaction.followup.send('❌ That does not look like a valid API key.')
        return

    model_msg = await _wait_dm(interaction, 'Finally send the model name, e.g. `gpt-5`.')
    if not model_msg:
        return
    model = model_msg.content.strip()[:200]
    if not model:
        await interaction.followup.send('❌ Model cannot be empty.')
        return

    _API_SESSIONS[user_id] = {
        'base_url': base,
        'key': key,
        'model': model,
        'history': [{'role': 'system', 'content': 'You are a helpful, concise assistant. Be accurate and ask for clarification when needed.'}],
    }
    await interaction.followup.send(
        f'✅ API connected.\n**Endpoint:** `{base}`\n**Model:** `{model}`\n\n'
        'You can now message me normally in this DM and I will send your messages to the configured API.\n'
        'Use `/api-clear` to forget the API key and conversation from this bot.'
    )

@bot.tree.command(name='api-clear', description='Forget your configured API key and private API conversation')
async def api_clear_command(interaction: discord.Interaction):
    _API_SESSIONS.pop(interaction.user.id, None)
    _API_LAST_REQUEST.pop(interaction.user.id, None)
    await interaction.response.send_message('🧹 Your API configuration and conversation have been removed from bot memory.')

@bot.tree.command(name='api-status', description='Show whether your private API chat is configured')
async def api_status_command(interaction: discord.Interaction):
    session = _API_SESSIONS.get(interaction.user.id)
    if not session:
        await interaction.response.send_message('❌ No API configured. Use `/api` in this DM.')
        return
    await interaction.response.send_message(f'✅ Connected to `{session["base_url"]}` using model `{session["model"]}`. The key is kept in memory only.')

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # In DMs, configured users can chat directly without a prefix.
    if message.guild is None and message.author.id in _API_SESSIONS and not message.content.startswith('/'):
        async with message.channel.typing():
            answer = await _api_chat(message.author.id, message.content)
        # Discord message limit is 2000 chars.
        if len(answer) <= 2000:
            await message.channel.send(answer)
        else:
            for i in range(0, len(answer), 1900):
                await message.channel.send(answer[i:i+1900])
        return
    await bot.process_commands(message)


LANG_MAP = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
    '.c': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp',
    '.h': 'c', '.hpp': 'cpp', '.cs': 'c_sharp', '.java': 'java',
    '.rs': 'rust', '.php': 'php', '.lua': 'lua', '.luau': 'lua',
    '.m': 'objc', '.mm': 'objc', '.swift': 'swift', '.go': 'go',
    '.rb': 'ruby', '.kt': 'kotlin', '.scala': 'scala',
    '.sh': 'bash', '.ps1': 'powershell', '.r': 'r',
    '.pl': 'perl', '.ex': 'elixir', '.erl': 'erlang',
    '.hs': 'haskell', '.ml': 'ocaml', '.zig': 'zig',
    '.nim': 'nim', '.v': 'v', '.d': 'd', '.sol': 'solidity'
}

REVERSE_LANG_MAP = {v: k for k, v in LANG_MAP.items()}

SECURITY_PATTERNS = {
    'integrity_check': [r'checksum', r'hash\s*\(', r'integrity', r'verify', r'validate', r'signature', r'HMAC', r'SHA\d+', r'MD5', r'CRC\d+', r'crc32', r'bcrypt', r'argon2'],
    'memory_guard': [r'ReadProcessMemory', r'WriteProcessMemory', r'VirtualProtect', r'VirtualAlloc', r'memcpy', r'memset', r'mmap', r'ptrace', r'ReadMemory', r'WriteMemory'],
    'speed_validation': [r'WalkSpeed', r'walkspeed', r'speed\s*[=<>]', r'velocity', r'movementSpeed', r'moveSpeed', r'CharacterMovement', r'MaxSpeed', r'GroundSpeed', r'AirSpeed', r'SwimSpeed', r'FlySpeed', r'acceleration'],
    'injection_detection': [r'DLL', r'dll', r'inject', r'LoadLibrary', r'GetProcAddress', r'dlopen', r'dlsym', r'module\s*load', r'hook', r'detour', r'trampoline'],
    'anti_debug': [r'IsDebuggerPresent', r'CheckRemoteDebuggerPresent', r'OutputDebugString', r'__debugbreak', r'int\s+3', r'PTRACE_TRACEME', r'AntiDebug', r'GetTickCount'],
    'anti_tamper': [r'anti.?tamper', r'code\s*integrity', r'section\s*hash', r'page\s*guard', r'self.?check', r'binary.?check', r'obfuscat', r'packer', r'VMProtect', r'Themida', r'Enigma', r'ASPack'],
    'network_validation': [r'server.?side', r'authoritative', r'reconcil', r'rollback', r'lag.?compensat', r'tick.?rate', r'sync', r'desync', r'heartbeat', r'keepalive', r'nonce'],
    'input_validation': [r'input.?sanitiz', r'rate.?limit', r'cooldown', r'throttle', r'debounce', r'max.?input', r'input.?clamp', r'clamp', r'normalize', r'saturate'],
    'encryption': [r'AES', r'RSA', r'ECC', r'encrypt', r'decrypt', r'cipher', r'key\s*=', r'IV\s*=', r'salt', r'padding'],
    'obfuscation': [r'xor', r'rotate', r'shift', r'encode', r'decode', r'base64', r'hex', r'mangle', r'scramble']
}

CATEGORY_LABELS = {
    'integrity_check': 'INTEGRITY / HASH CHECKS',
    'memory_guard': 'MEMORY PROTECTION',
    'speed_validation': 'SPEED / MOVEMENT VALIDATION',
    'injection_detection': 'INJECTION / HOOK DETECTION',
    'anti_debug': 'ANTI-DEBUG',
    'anti_tamper': 'ANTI-TAMPER / OBFUSCATION',
    'network_validation': 'NETWORK / SERVER AUTHORITY',
    'input_validation': 'INPUT VALIDATION / RATE LIMITS',
    'encryption': 'ENCRYPTION / CRYPTOGRAPHY',
    'obfuscation': 'OBFUSCATION TECHNIQUES'
}

# ==========================================================
# ADVANCED LUA OBFUSCATOR ENGINE (SCOPE-AWARE)
# ==========================================================
class IdentifierGenerator:
    LUA_KEYWORDS = {
        'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for',
        'function', 'goto', 'if', 'in', 'local', 'nil', 'not', 'or',
        'repeat', 'return', 'then', 'true', 'until', 'while'
    }
    
    def __init__(self, seed: int = 42):
        self.seed = seed
        self.counter = 0
        self.used_names: Set[str] = set(self.LUA_KEYWORDS)
        
    def generate(self) -> str:
        while True:
            self.counter += 1
            char = chr(97 + (self.counter % 26))
            num = (self.counter // 26) + 1
            candidate = f"_{char}{num}"
            
            if candidate not in self.used_names:
                self.used_names.add(candidate)
                return candidate
                
    def register_existing(self, name: str):
        self.used_names.add(name)

@dataclass
class Symbol:
    name: str
    declaration_node: Any
    scope: Any
    references: List[Any] = field(default_factory=list)
    is_safe_to_rename: bool = True

@dataclass
class Scope:
    parent: Optional[Any]
    node: Any
    symbols: Dict[str, Symbol] = field(default_factory=dict)
    children: List[Any] = field(default_factory=list)

class ScopeAnalyzer:
    def __init__(self, root_node: Any, source_code: str):
        self.root_node = root_node
        self.source_code = source_code
        self.global_scope = Scope(parent=None, node=root_node)
        self.current_scope = self.global_scope
        self.all_symbols: List[Symbol] = []
        self.rename_targets: List[Tuple[int, int, str]] = []

    def analyze(self, generator: IdentifierGenerator):
        self._build_scope_tree(self.root_node)
        self._resolve_and_mark(generator)

    def _push_scope(self, node: Any):
        new_scope = Scope(parent=self.current_scope, node=node)
        self.current_scope.children.append(new_scope)
        self.current_scope = new_scope

    def _pop_scope(self):
        if self.current_scope.parent:
            self.current_scope = self.current_scope.parent

    def _build_scope_tree(self, node: Any):
        node_type = node.type
        
        if node_type in ['chunk', 'function_declaration', 'function_definition', 
                         'do_statement', 'for_statement', 'for_in_statement', 'while_statement']:
            self._push_scope(node)

        if node_type == 'variable_declaration':
            for child in node.children:
                if child.type == 'variable_list':
                    for var in child.children:
                        if var.type == 'identifier':
                            name = var.text.decode('utf-8')
                            sym = Symbol(name=name, declaration_node=var, scope=self.current_scope)
                            self.current_scope.symbols[name] = sym
                            self.all_symbols.append(sym)

        elif node_type in ['function_declaration', 'function_definition']:
            for child in node.children:
                if child.type == 'parameters':
                    for param in child.children:
                        if param.type == 'identifier':
                            name = param.text.decode('utf-8')
                            sym = Symbol(name=name, declaration_node=param, scope=self.current_scope, is_safe_to_rename=True)
                            self.current_scope.symbols[name] = sym
                            self.all_symbols.append(sym)

        elif node_type in ['for_statement', 'for_in_statement']:
            for child in node.children:
                if child.type == 'variable_list':
                    for var in child.children:
                        if var.type == 'identifier':
                            name = var.text.decode('utf-8')
                            sym = Symbol(name=name, declaration_node=var, scope=self.current_scope)
                            self.current_scope.symbols[name] = sym
                            self.all_symbols.append(sym)

        elif node_type == 'identifier':
            name = node.text.decode('utf-8')
            
            parent = node.parent
            if parent and parent.type in ['field', 'table_field']:
                pass
            else:
                sym = self._resolve_symbol(name, self.current_scope)
                if sym:
                    sym.references.append(node)

        for child in node.children:
            self._build_scope_tree(child)

        if node_type in ['chunk', 'function_declaration', 'function_definition', 
                         'do_statement', 'for_statement', 'for_in_statement', 'while_statement']:
            self._pop_scope()

    def _resolve_symbol(self, name: str, scope: Any) -> Optional[Symbol]:
        current = scope
        while current:
            if name in current.symbols:
                return current.symbols[name]
            current = current.parent
        return None

    def _resolve_and_mark(self, generator: IdentifierGenerator):
        for sym in self.all_symbols:
            generator.register_existing(sym.name)
            
        def register_all_identifiers(node):
            if node.type == 'identifier':
                generator.register_existing(node.text.decode('utf-8'))
            for child in node.children:
                register_all_identifiers(child)
        register_all_identifiers(self.root_node)

        for sym in self.all_symbols:
            if sym.is_safe_to_rename and len(sym.references) > 0:
                new_name = generator.generate()
                self.rename_targets.append((sym.declaration_node.start_byte, sym.declaration_node.end_byte, new_name))
                for ref in sym.references:
                    self.rename_targets.append((ref.start_byte, ref.end_byte, new_name))


# ==========================================================
# ADVANCED, CONSERVATIVE LUA/LUAU TRANSFORMATION LAYER
# ==========================================================
# This layer intentionally preserves the original public classes and commands.
# It adds a lexer-first, scope-aware transformer that never rewrites inside
# strings/comments/table keys and rolls back on validation failure.

@dataclass
class LuaToken:
    kind: str
    value: str
    start: int
    end: int
    line: int
    column: int


class LuaLexer:
    KEYWORDS = {
        'and','break','do','else','elseif','end','false','for','function','goto',
        'if','in','local','nil','not','or','repeat','return','then','true','until','while'
    }

    def __init__(self, source: str):
        self.source = source
        self.tokens: List[LuaToken] = []

    def tokenize(self) -> List[LuaToken]:
        s = self.source
        n = len(s)
        i = 0
        line = 1
        col = 1

        def advance(segment: str):
            nonlocal line, col
            parts = segment.splitlines(True)
            if len(parts) > 1:
                line += len(parts) - 1
                col = len(parts[-1]) + 1
            else:
                col += len(segment)

        while i < n:
            c = s[i]

            if c in ' \t\r\n':
                j = i + 1
                while j < n and s[j] in ' \t\r\n':
                    j += 1
                advance(s[i:j]); i = j
                continue

            # Lua comments, including long comments.
            if s.startswith('--', i):
                start, sl, sc = i, line, col
                if s.startswith('--[[', i):
                    end = s.find(']]', i + 4)
                    j = n if end < 0 else end + 2
                else:
                    end = s.find('\n', i + 2)
                    j = n if end < 0 else end
                value = s[i:j]
                self.tokens.append(LuaToken('comment', value, start, j, sl, sc))
                advance(value); i = j
                continue

            # Quoted strings.
            if c in ("'", '"'):
                start, sl, sc = i, line, col
                quote = c
                j = i + 1
                escaped = False
                while j < n:
                    ch = s[j]
                    if escaped:
                        escaped = False
                    elif ch == '\\':
                        escaped = True
                    elif ch == quote:
                        j += 1
                        break
                    j += 1
                value = s[i:j]
                self.tokens.append(LuaToken('string', value, start, j, sl, sc))
                advance(value); i = j
                continue

            # Long bracket strings.
            if c == '[':
                m = re.match(r'\[(=*)\[', s[i:])
                if m:
                    opener = m.group(0)
                    closer = ']' + m.group(1) + ']'
                    end = s.find(closer, i + len(opener))
                    j = n if end < 0 else end + len(closer)
                    value = s[i:j]
                    self.tokens.append(LuaToken('string', value, i, j, line, col))
                    advance(value); i = j
                    continue

            # Identifiers / keywords.
            if c.isalpha() or c == '_':
                j = i + 1
                while j < n and (s[j].isalnum() or s[j] == '_'):
                    j += 1
                value = s[i:j]
                kind = 'keyword' if value in self.KEYWORDS else 'identifier'
                self.tokens.append(LuaToken(kind, value, i, j, line, col))
                advance(value); i = j
                continue

            # Numbers.
            if c.isdigit() or (c == '.' and i + 1 < n and s[i + 1].isdigit()):
                m = re.match(
                    r'(?:0[xX][0-9A-Fa-f]+(?:\.[0-9A-Fa-f]*)?|'
                    r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',
                    s[i:]
                )
                value = m.group(0) if m else c
                j = i + len(value)
                self.tokens.append(LuaToken('number', value, i, j, line, col))
                advance(value); i = j
                continue

            # Multi-character operators.
            matched = None
            for op in ('...', '==', '~=', '<=', '>=', '::', '->', '//', '<<', '>>', '..'):
                if s.startswith(op, i):
                    matched = op
                    break
            if matched:
                j = i + len(matched)
                self.tokens.append(LuaToken('symbol', matched, i, j, line, col))
                advance(matched); i = j
                continue

            self.tokens.append(LuaToken('symbol', c, i, i + 1, line, col))
            advance(c); i += 1

        return self.tokens


@dataclass
class LuaBinding:
    name: str
    new_name: str
    declaration_index: int
    scope_id: int
    references: List[int] = field(default_factory=list)


@dataclass
class LuaScopeFrame:
    scope_id: int
    parent_id: Optional[int]
    start_index: int
    end_index: int
    kind: str
    bindings: Dict[str, LuaBinding] = field(default_factory=dict)


class AdvancedLuaRenamer:
    """
    Conservative lexical/scope-aware renamer.

    It intentionally renames only local declarations and parameters whose
    scope can be established without guessing. Globals, table fields,
    method names, labels, strings and comments are left untouched.
    """

    def __init__(self, source: str, seed: int = 1337):
        self.source = source
        self.tokens = LuaLexer(source).tokenize()
        self.generator = IdentifierGenerator(seed=seed)
        self.scopes: List[LuaScopeFrame] = []
        self.replacements: List[Tuple[int, int, str]] = []
        self.diagnostics: List[str] = []

    def _is_sig(self, i: int) -> bool:
        return 0 <= i < len(self.tokens) and self.tokens[i].kind not in ('comment',)

    def _next_sig(self, i: int) -> Optional[int]:
        i += 1
        while i < len(self.tokens):
            if self.tokens[i].kind != 'comment':
                return i
            i += 1
        return None

    def _prev_sig(self, i: int) -> Optional[int]:
        i -= 1
        while i >= 0:
            if self.tokens[i].kind != 'comment':
                return i
            i -= 1
        return None

    def _scope_at(self, index: int) -> Optional[LuaScopeFrame]:
        candidates = [
            s for s in self.scopes
            if s.start_index <= index <= s.end_index
        ]
        if not candidates:
            return self.scopes[0] if self.scopes else None
        return max(candidates, key=lambda x: x.start_index)

    def _find_matching_end(self, start: int) -> Optional[int]:
        depth = 0
        i = start
        while i < len(self.tokens):
            t = self.tokens[i]
            if t.kind == 'keyword':
                if t.value in ('function', 'do', 'then', 'for', 'while', 'repeat'):
                    depth += 1
                elif t.value == 'end':
                    if depth == 0:
                        return i
                    depth -= 1
                elif t.value == 'until' and depth == 0:
                    return i
            i += 1
        return None

    def _matching_block_end(self, start: int, initial: Optional[str] = None) -> int:
        # Conservative block matcher. If uncertain, end at file end and
        # validation will prevent unsafe replacements from being committed.
        stack = [initial] if initial else []
        i = start
        while i < len(self.tokens):
            t = self.tokens[i]
            if t.kind == 'keyword':
                if t.value in ('function', 'do', 'for', 'while', 'if'):
                    stack.append(t.value)
                elif t.value == 'repeat':
                    stack.append('repeat')
                elif t.value == 'end':
                    if stack:
                        stack.pop()
                        if not stack:
                            return i
                elif t.value == 'until':
                    if stack and stack[-1] == 'repeat':
                        stack.pop()
                        if not stack:
                            return i
            i += 1
        return len(self.tokens) - 1

    def _add_scope(self, start: int, end: int, kind: str, parent: Optional[int]) -> int:
        sid = len(self.scopes)
        self.scopes.append(LuaScopeFrame(sid, parent, start, end, kind))
        return sid

    def _find_parent_scope(self, index: int) -> Optional[int]:
        candidates = [
            s for s in self.scopes
            if s.start_index <= index <= s.end_index
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.start_index).scope_id

    def _build_scopes(self):
        if not self.tokens:
            return

        root = self._add_scope(0, len(self.tokens) - 1, 'chunk', None)

        # Add function body scopes. We deliberately avoid trying to model
        # every Lua grammar production; uncertain constructs remain untouched.
        for i, t in enumerate(self.tokens):
            if t.kind == 'keyword' and t.value == 'function':
                close_paren = None
                depth = 0
                j = self._next_sig(i)
                while j is not None and j < len(self.tokens):
                    if self.tokens[j].value == '(':
                        depth += 1
                    elif self.tokens[j].value == ')':
                        depth -= 1
                        if depth == 0:
                            close_paren = j
                            break
                    j = self._next_sig(j)
                if close_paren is None:
                    continue

                body_end = self._matching_block_end(close_paren + 1, 'function')
                self._add_scope(close_paren + 1, body_end, 'function', root)

        # Re-parent nested function scopes by containment. This is essential
        # for closure/upvalue resolution: a nested function must see bindings
        # from its lexical parent instead of jumping directly to the chunk.
        for child in self.scopes:
            if child.scope_id == root:
                continue
            parents = [
                p for p in self.scopes
                if p.scope_id != child.scope_id
                and p.start_index <= child.start_index
                and p.end_index >= child.end_index
            ]
            if parents:
                parent = max(parents, key=lambda p: p.start_index)
                child.parent_id = parent.scope_id

        self.scopes.sort(key=lambda s: (s.start_index, -s.end_index))

    def _scope_for_index(self, index: int) -> LuaScopeFrame:
        candidates = [
            s for s in self.scopes
            if s.start_index <= index <= s.end_index
        ]
        return max(candidates, key=lambda s: s.start_index) if candidates else self.scopes[0]

    def _reserve_names(self):
        for t in self.tokens:
            if t.kind == 'identifier':
                self.generator.register_existing(t.value)

    def _add_binding(self, token_index: int, scope: LuaScopeFrame):
        token = self.tokens[token_index]
        if token.kind != 'identifier':
            return None
        if token.value in scope.bindings:
            return scope.bindings[token.value]
        new_name = self.generator.generate()
        binding = LuaBinding(token.value, new_name, token_index, scope.scope_id)
        scope.bindings[token.value] = binding
        self.replacements.append((token.start, token.end, new_name))
        return binding

    def _resolve(self, name: str, scope: LuaScopeFrame) -> Optional[LuaBinding]:
        current = scope
        seen = set()
        while current and current.scope_id not in seen:
            seen.add(current.scope_id)
            if name in current.bindings:
                return current.bindings[name]
            if current.parent_id is None:
                break
            current = self.scopes[current.parent_id]
        return None

    def _identifier_is_table_key(self, i: int) -> bool:
        prev_i = self._prev_sig(i)
        next_i = self._next_sig(i)
        if prev_i is not None and self.tokens[prev_i].value == '.':
            return True
        if prev_i is not None and self.tokens[prev_i].value == '::':
            return True
        if next_i is not None and self.tokens[next_i].value == '::':
            return True
        # key = value inside a table constructor
        if next_i is not None and self.tokens[next_i].value == '=':
            if prev_i is not None and self.tokens[prev_i].value in ('{', ','):
                return True
        return False

    def _collect_parameters(self, function_index: int, body_scope: LuaScopeFrame):
        # Find the first parameter list following 'function'.
        i = self._next_sig(function_index)
        while i is not None and i < len(self.tokens):
            if self.tokens[i].value == '(':
                break
            if self.tokens[i].value in ('do', 'end', ';'):
                return
            i = self._next_sig(i)
        if i is None or self.tokens[i].value != '(':
            return
        depth = 1
        j = i + 1
        while j < len(self.tokens) and depth:
            t = self.tokens[j]
            if t.kind == 'comment':
                j += 1
                continue
            if t.value == '(':
                depth += 1
            elif t.value == ')':
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and t.kind == 'identifier':
                prev_i = self._prev_sig(j)
                if prev_i is None or self.tokens[prev_i].value in ('(', ','):
                    self._add_binding(j, body_scope)
            j += 1

    def _collect_local_declaration(self, i: int, scope: LuaScopeFrame):
        # local <name>[, <name>...] [= ...]
        j = self._next_sig(i)
        if j is None:
            return
        if self.tokens[j].kind == 'keyword' and self.tokens[j].value == 'function':
            name_i = self._next_sig(j)
            if name_i is not None and self.tokens[name_i].kind == 'identifier':
                self._add_binding(name_i, scope)
            return

        while j is not None and j < len(self.tokens):
            t = self.tokens[j]
            if t.kind != 'identifier':
                break
            self._add_binding(j, scope)
            nxt = self._next_sig(j)
            if nxt is None or self.tokens[nxt].value != ',':
                break
            j = self._next_sig(nxt)

    def _collect_for_declaration(self, i: int, scope: LuaScopeFrame):
        j = self._next_sig(i)
        while j is not None and j < len(self.tokens):
            t = self.tokens[j]
            if t.kind != 'identifier':
                break
            self._add_binding(j, scope)
            nxt = self._next_sig(j)
            if nxt is None or self.tokens[nxt].value not in (',',):
                break
            j = self._next_sig(nxt)

    def transform(self) -> Tuple[str, Dict[str, Any]]:
        if not self.tokens:
            return self.source, {'changed': False, 'replacements': 0, 'warnings': []}

        self._build_scopes()
        self._reserve_names()

        # Declarations.
        for i, t in enumerate(self.tokens):
            if t.kind != 'keyword':
                continue
            scope = self._scope_for_index(i)

            if t.value == 'local':
                self._collect_local_declaration(i, scope)
            elif t.value == 'for':
                self._collect_for_declaration(i, scope)

        # Function parameters belong to the nearest function scope.
        for i, t in enumerate(self.tokens):
            if t.kind == 'keyword' and t.value == 'function':
                body = None
                candidates = [s for s in self.scopes if s.kind == 'function' and s.start_index >= i]
                if candidates:
                    body = min(candidates, key=lambda s: s.start_index)
                if body:
                    self._collect_parameters(i, body)

        # References.
        for i, t in enumerate(self.tokens):
            if t.kind != 'identifier':
                continue
            if self._identifier_is_table_key(i):
                continue

            scope = self._scope_for_index(i)
            binding = self._resolve(t.value, scope)
            if not binding:
                continue

            # Don't touch declaration twice.
            if i == binding.declaration_index:
                continue

            binding.references.append(i)
            self.replacements.append((t.start, t.end, binding.new_name))

        # Deduplicate exact replacement ranges.
        unique = {}
        for start, end, name in self.replacements:
            unique[(start, end)] = name
        replacements = [(s, e, n) for (s, e), n in unique.items()]

        # Apply right-to-left to preserve offsets.
        result = self.source
        for start, end, name in sorted(replacements, reverse=True):
            result = result[:start] + name + result[end:]

        return result, {
            'changed': result != self.source,
            'replacements': len(replacements),
            'scopes': len(self.scopes),
            'warnings': self.diagnostics,
        }


class AdvancedLuaObfuscatorEngine:
    def __init__(self, source_code: str, seed: Optional[int] = None):
        self.original_source = source_code
        self.seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        self.validation_errors: List[str] = []

    def _tree_sitter_valid(self, source: str) -> bool:
        if not (TREE_SITTER_AVAILABLE and LUA_PARSER_AVAILABLE):
            # The lexer itself is still guaranteed not to rewrite strings/comments.
            return True
        try:
            parser = get_parser('lua')
            tree = parser.parse(source.encode('utf-8'))
            def has_error(node):
                if node.type == 'ERROR':
                    return True
                return any(has_error(c) for c in node.children)
            return not has_error(tree.root_node)
        except Exception as exc:
            self.validation_errors.append(str(exc))
            return False

    def obfuscate(self) -> str:
        # Transactional pipeline: every transformation is validated before commit.
        try:
            renamer = AdvancedLuaRenamer(self.original_source, seed=self.seed)
            candidate, metadata = renamer.transform()

            if not self._tree_sitter_valid(candidate):
                self.validation_errors.append("Parser rejected transformed source; rollback applied.")
                return self.original_source

            return candidate
        except Exception as exc:
            self.validation_errors.append(f"Transformation failed: {exc}")
            return self.original_source


# ==========================================================
# OLD OBFUSCATION DETECTOR (for analysis)
# ==========================================================
class ObfuscationDetector:
    def __init__(self, content):
        self.content = content
        self.is_obfuscated = False
        self.obfuscation_techniques = []
        self.confidence = 0

    def detect(self):
        score = 0
        
        if re.search(r'[A-Za-z0-9+/]{50,}={0,2}', self.content):
            self.obfuscation_techniques.append('Base64 encoded strings')
            score += 20
        
        if re.search(r'\\x[0-9a-fA-F]{2}', self.content):
            self.obfuscation_techniques.append('Hex encoded strings')
            score += 15
        
        if re.search(r'\\u[0-9a-fA-F]{4}', self.content):
            self.obfuscation_techniques.append('Unicode encoded strings')
            score += 15
        
        var_names = re.findall(r'(?:var|let|const|local|function)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)', self.content)
        if var_names:
            avg_length = sum(len(v) for v in var_names) / len(var_names)
            if avg_length < 3:
                self.obfuscation_techniques.append('Extremely short variable names')
                score += 25
        
        if re.search(r'(?:eval|exec|Function|loadstring|_G)', self.content):
            self.obfuscation_techniques.append('Dynamic code execution')
            score += 20
        
        if re.search(r'string\.char|String\.fromCharCode|chr\(', self.content):
            self.obfuscation_techniques.append('Character code obfuscation')
            score += 15
        
        if re.search(r'(?:\+\+|--)[\s]*[a-zA-Z_$]+', self.content):
            self.obfuscation_techniques.append('Increment/decrement obfuscation')
            score += 10
        
        if re.search(r'!\[\]\+\[\]', self.content) or re.search(r'\[\]\[\'\w+\'\]', self.content):
            self.obfuscation_techniques.append('JavaScript bracket notation obfuscation')
            score += 25
        
        if re.search(r'function\s*\([a-z]\)\s*\{[^\}]*\}', self.content):
            self.obfuscation_techniques.append('Single-letter parameter functions')
            score += 10
        
        if re.search(r'(?:0x[0-9a-fA-F]+)', self.content):
            hex_count = len(re.findall(r'0x[0-9a-fA-F]+', self.content))
            if hex_count > 10:
                self.obfuscation_techniques.append('Hexadecimal number obfuscation')
                score += 15
        
        if re.search(r'(?:split|join|concat)\s*\(', self.content):
            self.obfuscation_techniques.append('String manipulation obfuscation')
            score += 10
        
        if re.search(r'(?:self|_ENV|_G)\[.*?\]', self.content):
            self.obfuscation_techniques.append('Global table access obfuscation')
            score += 15
        
        self.confidence = min(score, 100)
        self.is_obfuscated = score >= 30
        
        return self.is_obfuscated, self.obfuscation_techniques, self.confidence


# ==========================================================
# ADVANCED OBFUSCATION DETECTION / SAFE DEOBF ANALYSIS
# ==========================================================

class AdvancedObfuscationDetector(ObfuscationDetector):
    ENGINE_SIGNATURES = {
        'Prometheus': [
            r'Prometheus', r'LPH!', r'local\s+LPH', r'protected\s+call',
            r'string\.char\s*\(', r'bit32\.', r'getfenv\s*\('
        ],
        'MoonSec': [
            r'MoonSec', r'MoonSecV\d', r'__MSEC', r'loadstring\s*\(',
            r'_ENV\s*\[', r'setfenv\s*\('
        ],
        'IronBrew2': [
            r'IronBrew', r'IronBrew2', r'bit32\.', r'VM\s*=', r'VIP\s*=',
            r'local\s+VIP', r'local\s+INS'
        ],
        'MoonVeil': [
            r'MoonVeil', r'__MV', r'MV_', r'Veil'
        ],
        'ChaoticGood': [
            r'ChaoticGood', r'chaotic', r'control.?flow'
        ],
    }

    def detect_engines(self) -> List[Dict[str, Any]]:
        results = []
        source = self.content
        for engine, patterns in self.ENGINE_SIGNATURES.items():
            hits = []
            for pattern in patterns:
                try:
                    if re.search(pattern, source, re.IGNORECASE):
                        hits.append(pattern)
                except re.error:
                    continue
            if hits:
                # Multiple independent signatures raise confidence, but never
                # claim certainty from one generic construct.
                confidence = min(0.99, 0.35 + 0.13 * len(hits))
                results.append({
                    'engine': engine,
                    'confidence': confidence,
                    'signatures': hits,
                    'warnings': []
                })
        results.sort(key=lambda x: x['confidence'], reverse=True)
        return results


class SafeLuaConstantFolder:
    """Conservative source-level cleanup; never executes arbitrary code."""

    @staticmethod
    def fold_string_char(source: str) -> str:
        # Only fold literal numeric string.char calls.
        pattern = re.compile(
            r'string\.char\s*\(\s*((?:0|[1-9]\d*)(?:\s*,\s*(?:0|[1-9]\d*))*)\s*\)'
        )

        def repl(match):
            raw = match.group(1)
            try:
                nums = [int(x.strip()) for x in raw.split(',')]
                if not nums or any(n < 0 or n > 255 for n in nums):
                    return match.group(0)
                value = ''.join(chr(n) for n in nums)
                # Lua-compatible double quoted literal.
                escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
                return '"' + escaped + '"'
            except Exception:
                return match.group(0)

        return pattern.sub(repl, source)


class AdvancedDeobfuscatorEngine:
    """
    Defensive, conservative deobfuscation pipeline.

    It detects likely obfuscation families and performs only semantics-safe
    normalization/constant folding. It does not attempt to defeat protected
    virtual machines, anti-tamper checks, or proprietary runtime protections.
    """

    def __init__(self):
        self.detector_cache = {}

    def analyze(self, source: str) -> Dict[str, Any]:
        detector = AdvancedObfuscationDetector(source)
        is_obs, techniques, confidence = detector.detect()
        engines = detector.detect_engines()

        best = engines[0] if engines else {
            'engine': 'Generic Lua',
            'confidence': 0.50,
            'signatures': ['No engine-specific signature']
        }

        return {
            'is_obfuscated': is_obs,
            'techniques': techniques,
            'confidence': confidence / 100.0,
            'engine': best['engine'],
            'engine_confidence': best['confidence'],
            'signatures': best.get('signatures', []),
            'candidates': engines,
            'warnings': [
                'Engine detection is heuristic and may produce false positives.',
                'Only conservative source-level transformations are applied.'
            ]
        }

    def transform(self, source: str) -> Tuple[str, Dict[str, Any]]:
        meta = self.analyze(source)
        candidate = source
        changes = []

        folded = SafeLuaConstantFolder.fold_string_char(candidate)
        if folded != candidate:
            candidate = folded
            changes.append('literal string.char folding')

        # Normalize excessive blank lines only; never alter tokens otherwise.
        normalized = re.sub(r'\n[ \t]*\n[ \t]*\n+', '\n\n', candidate)
        if normalized != candidate:
            candidate = normalized
            changes.append('whitespace normalization')

        meta['changes'] = changes
        meta['output_size'] = len(candidate)
        return candidate, meta


class LuaObfuscatorEngine:
    def __init__(self, source_code: str):
        self.original_source = source_code
        self.current_source = source_code
        if TREE_SITTER_AVAILABLE and LUA_PARSER_AVAILABLE:
            try:
                self.parser = get_parser('lua')
                self.language = get_language('lua')
                self.has_parser = True
            except:
                self.has_parser = False
        else:
            self.has_parser = False

    def obfuscate(self) -> str:
        if not self.has_parser:
            return self._fallback_obfuscate()
            
        try:
            tree = self.parser.parse(bytes(self.current_source, "utf8"))
            
            generator = IdentifierGenerator()
            analyzer = ScopeAnalyzer(tree.root_node, self.current_source)
            analyzer.analyze(generator)
            
            new_source = self._apply_replacements(self.current_source, analyzer.rename_targets)
            
            if self._validate_syntax(new_source):
                return new_source
            else:
                return self.original_source
        except Exception as e:
            print(f"Obfuscation error: {e}")
            return self.original_source

    def _fallback_obfuscate(self) -> str:
        lines = self.current_source.split('\n')
        obfuscated_lines = []
        var_map = {}
        
        def get_random_name():
            return '_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 8)))
        
        for line in lines:
            if line.strip().startswith('--') or line.strip() == '':
                obfuscated_lines.append(line)
                continue
            
            new_line = line
            
            func_matches = re.findall(r'function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', line)
            for func_name in func_matches:
                if func_name not in var_map:
                    var_map[func_name] = get_random_name()
                new_line = re.sub(r'\b' + func_name + r'\b', var_map[func_name], new_line)
            
            var_matches = re.findall(r'local\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=', line)
            for var_name in var_matches:
                if var_name not in var_map:
                    var_map[var_name] = get_random_name()
                new_line = re.sub(r'\b' + var_name + r'\b', var_map[var_name], new_line)
            
            obfuscated_lines.append(new_line)
        
        return '\n'.join(obfuscated_lines)

    def _apply_replacements(self, source: str, targets: List[Tuple[int, int, str]]) -> str:
        sorted_targets = sorted(targets, key=lambda x: x[0], reverse=True)
        result = bytearray(source.encode('utf-8'))
        
        for start, end, new_name in sorted_targets:
            new_bytes = new_name.encode('utf-8')
            result[start:end] = new_bytes
            
        return result.decode('utf-8')

    def _validate_syntax(self, source: str) -> bool:
        if not self.has_parser:
            return True
        try:
            tree = self.parser.parse(bytes(source, "utf8"))
            def has_errors(node):
                if node.type == 'ERROR':
                    return True
                for child in node.children:
                    if has_errors(child):
                        return True
                return False
            return not has_errors(tree.root_node)
        except:
            return False

class DeobfuscatorAdapter:
    def detect(self, source: str) -> Dict[str, Any]:
        raise NotImplementedError
        
    def deobfuscate(self, source: str) -> str:
        raise NotImplementedError

class PrometheusAdapter(DeobfuscatorAdapter):
    def detect(self, source: str) -> Dict[str, Any]:
        signatures = []
        if re.search(r'local\s+[a-zA-Z0-9_]+\s*=\s*string\.char', source):
            signatures.append("string.char constant decoding")
        if re.search(r'while\s+true\s+do', source) and source.count('end') > 50:
            signatures.append("heavy while-true control flow")
            
        confidence = len(signatures) * 0.4
        return {
            "engine": "Prometheus",
            "confidence": min(confidence, 0.99),
            "signatures": signatures
        }

    def deobfuscate(self, source: str) -> str:
        return source

class MoonSecAdapter(DeobfuscatorAdapter):
    def detect(self, source: str) -> Dict[str, Any]:
        signatures = []
        if '_ENV' in source or 'setfenv' in source:
            signatures.append("environment manipulation")
        if re.search(r'loadstring|load', source):
            signatures.append("dynamic code loading")
            
        return {
            "engine": "MoonSec",
            "confidence": len(signatures) * 0.35,
            "signatures": signatures
        }

    def deobfuscate(self, source: str) -> str:
        return source

class GenericLuaAdapter(DeobfuscatorAdapter):
    def detect(self, source: str) -> Dict[str, Any]:
        return {"engine": "Generic", "confidence": 0.5, "signatures": ["fallback"]}

    def deobfuscate(self, source: str) -> str:
        return re.sub(r'\n\s*\n', '\n', source)

class DeobfuscatorEngine:
    def __init__(self):
        self.adapters = [
            PrometheusAdapter(),
            MoonSecAdapter(),
            GenericLuaAdapter()
        ]

    def analyze_and_deobfuscate(self, source: str) -> Tuple[str, Dict[str, Any]]:
        best_adapter = None
        best_confidence = 0.0
        best_meta = {}

        for adapter in self.adapters:
            meta = adapter.detect(source)
            if meta['confidence'] > best_confidence:
                best_confidence = meta['confidence']
                best_adapter = adapter
                best_meta = meta

        if best_confidence < 0.6:
            best_adapter = GenericLuaAdapter()
            best_meta = best_adapter.detect(source)
        
        result = best_adapter.deobfuscate(source)
        return result, best_meta


class SourceAnalyzer:
    def __init__(self, content, ext):
        self.content = content
        self.ext = ext
        self.lang = LANG_MAP.get(ext, 'unknown')
        self.lines = content.splitlines()
        self.total_lines = len(self.lines)
        self.total_chars = len(content)

    def extract_strings(self):
        return re.findall(r'["\']([^"\']{3,50})["\']', self.content)[:30]

    def extract_constants(self):
        return list(set(re.findall(r'\b(\d+\.?\d*)\b', self.content)))[:50]

class ASTBuilder:
    def __init__(self, content, lang):
        self.content = content
        self.lang = lang
        self.tree = None
        self.root = None
        if TREE_SITTER_AVAILABLE and lang != 'unknown':
            try:
                parser = get_parser(lang)
                self.tree = parser.parse(bytes(content, "utf8"))
                self.root = self.tree.root_node
            except Exception:
                pass

    def get_root(self):
        return self.root

class SymbolExtractor:
    def __init__(self, root_node, content):
        self.root = root_node
        self.content = content
        self.functions = []
        self.classes = []
        self.variables = []
        self.imports = []
        self.decorators = []
        self.macros = []

    def extract(self):
        if not self.root:
            self._regex_fallback()
            return
        self._walk(self.root, 0)

    def _walk(self, node, depth):
        node_type = node.type
        if node_type in ['function_definition', 'method_definition', 'function_declaration', 'function_item', 'method_declaration', 'constructor_declaration']:
            name_node = node.child_by_field_name('name')
            if name_node:
                self.functions.append({
                    'name': name_node.text.decode('utf8', errors='ignore'),
                    'line': node.start_point[0] + 1,
                    'size': node.end_point[0] - node.start_point[0],
                    'params': [],
                    'calls': [],
                    'complexity': 0,
                    'depth': depth
                })
        elif node_type in ['class_definition', 'class_declaration', 'struct_item', 'interface_declaration', 'impl_item', 'enum_item']:
            name_node = node.child_by_field_name('name')
            if name_node:
                self.classes.append({
                    'name': name_node.text.decode('utf8', errors='ignore'),
                    'line': node.start_point[0] + 1,
                    'type': node_type
                })
        elif node_type in ['import_statement', 'import_from_statement', 'using_directive', 'use_declaration', 'include_directive', 'require_statement']:
            self.imports.append(node.text.decode('utf8', errors='ignore')[:100])
        elif node_type in ['decorator', 'annotation']:
            self.decorators.append(node.text.decode('utf8', errors='ignore')[:50])
        elif node_type in ['macro_definition', 'preproc_def']:
            self.macros.append(node.text.decode('utf8', errors='ignore')[:50])
        
        for child in node.children:
            self._walk(child, depth + 1)

    def _regex_fallback(self):
        func_patterns = [
            r'(?:def|function|func|fn|void|int|float|double|public|private|protected|static|async|local)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*function',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*function'
        ]
        for p in func_patterns:
            for f in re.findall(p, self.content):
                self.functions.append({'name': f, 'line': 0, 'size': 0, 'params': [], 'calls': [], 'complexity': 0, 'depth': 0})
        
        imp = re.findall(r'(?:import|require|include|using|#include)\s+[<"\']([^>"\']+)[>"\']', self.content)
        self.imports = imp[:30]

class ControlFlowAnalyzer:
    def __init__(self, content):
        self.content = content
        self.branches = 0
        self.loops = 0
        self.early_returns = 0
        self.error_paths = 0
        self.recursion = []

    def analyze(self, functions):
        self.branches = len(re.findall(r'\b(if|else|switch|case|match)\b', self.content, re.IGNORECASE))
        self.loops = len(re.findall(r'\b(for|while|do|loop|repeat)\b', self.content, re.IGNORECASE))
        self.early_returns = len(re.findall(r'\breturn\b', self.content))
        self.error_paths = len(re.findall(r'\b(catch|except|rescue|err|error)\b', self.content, re.IGNORECASE))
        
        func_names = [f['name'] for f in functions]
        for fn in functions:
            if self.content.count(fn['name']) > 1:
                self.recursion.append(fn['name'])

class DataFlowAnalyzer:
    def __init__(self, content):
        self.content = content
        self.transformations = 0
        self.definitions = []
        self.uses = []

    def analyze(self):
        self.transformations = len(re.findall(r'[\+\-\*/%]=|<<=|>>=|&=|\|=|\^=', self.content))

class CallGraphBuilder:
    def __init__(self, functions, content):
        self.functions = functions
        self.content = content
        self.graph = defaultdict(list)
        self.callers = defaultdict(list)
        self.callees = defaultdict(list)

    def build(self):
        func_names = [f['name'] for f in self.functions]
        for fn in self.functions:
            for target in func_names:
                if target != fn['name'] and target in self.content:
                    self.graph[fn['name']].append(target)
                    self.callers[target].append(fn['name'])
                    self.callees[fn['name']].append(target)

class DependencyAnalyzer:
    def __init__(self, imports):
        self.imports = imports
        self.external_libs = []
        self.frameworks = []
        self.internal_modules = []

    def analyze(self):
        known_frameworks = ['discord', 'flask', 'django', 'react', 'vue', 'angular', 'spring', 'express', 'fastapi', 'torch', 'tensorflow']
        for imp in self.imports:
            lib_name = imp.split('.')[-1].split('/')[-1].strip('<>"\' ')
            if lib_name in known_frameworks:
                self.frameworks.append(lib_name)
            elif lib_name.startswith('.'):
                self.internal_modules.append(lib_name)
            else:
                self.external_libs.append(lib_name)

class BehavioralClassifier:
    def __init__(self, functions, content):
        self.functions = functions
        self.content = content
        self.categories = defaultdict(list)

    def classify(self):
        behavior_patterns = {
            'Networking': [r'socket', r'connect', r'send', r'recv', r'request', r'fetch', r'http'],
            'File I/O': [r'open', r'read', r'write', r'close', r'fopen', r'fprintf'],
            'Database': [r'sql', r'query', r'execute', r'cursor', r'mongo', r'redis', r'database'],
            'Cryptography': [r'encrypt', r'decrypt', r'hash', r'sha', r'aes', r'rsa', r'cipher', r'sign'],
            'Authentication': [r'login', r'logout', r'auth', r'token', r'jwt', r'session', r'password'],
            'UI/Rendering': [r'render', r'draw', r'paint', r'ui', r'widget', r'component', r'canvas'],
            'Physics/Movement': [r'velocity', r'acceleration', r'force', r'mass', r'gravity', r'collision', r'move', r'walk', r'jump'],
            'AI/Machine Learning': [r'model', r'train', r'predict', r'neural', r'tensor', r'inference'],
            'Security': [r'validate', r'sanitize', r'filter', r'guard', r'protect', r'check', r'verify']
        }
        
        for fn in self.functions:
            for cat, patterns in behavior_patterns.items():
                for p in patterns:
                    if re.search(p, fn['name'], re.IGNORECASE):
                        if fn['name'] not in self.categories[cat]:
                            self.categories[cat].append(fn['name'])
                        break

class SecurityAnalyzer:
    def __init__(self, content):
        self.content = content
        self.findings = []
        self.weaknesses = []

    def analyze(self):
        for cat, patterns in SECURITY_PATTERNS.items():
            matches = []
            for p in patterns:
                found = re.findall(p, self.content, re.IGNORECASE)
                if found:
                    matches.extend(found)
            if matches:
                unique = list(set(matches))[:10]
                self.findings.append({'category': CATEGORY_LABELS.get(cat, cat.upper()), 'evidence': unique})

        weakness_patterns = {
            'Unsafe Input': [r'eval\(', r'exec\(', r'system\(', r'popen'],
            'Hardcoded Secrets': [r'password\s*=\s*["\'][^"\']+["\']', r'api_key\s*=\s*["\'][^"\']+["\']', r'token\s*=\s*["\'][^"\']+["\']'],
            'Weak Crypto': [r'\bMD5\b', r'\bSHA1\b', r'\bDES\b', r'\bRC4\b']
        }

        for cat, patterns in weakness_patterns.items():
            for p in patterns:
                matches = re.findall(p, self.content, re.IGNORECASE)
                if matches:
                    self.weaknesses.append({'category': cat, 'evidence': list(set(matches))[:3]})

class GameAnalyzer:
    def __init__(self, content):
        self.content = content
        self.game_features = []

    def analyze(self):
        game_patterns = {
            'Player Movement': [r'WalkSpeed', r'movementSpeed', r'velocity', r'CharacterMovement', r'jump', r'fly'],
            'Networking/Replication': [r'replicate', r'interpolate', r'predict', r'lag.?compensat', r'tick', r'server.?side'],
            'Damage/Health': [r'health', r'damage', r'hitbox', r'armor', r'heal', r'die', r'respawn'],
            'Inventory': [r'inventory', r'item', r'equip', r'slot', r'backpack', r'loot'],
            'Anti-Cheat': [r'anticheat', r'exploit', r'cheat', r'ban', r'kick', r'validation', r'speed.?hack']
        }
        
        for feature, patterns in game_patterns.items():
            for p in patterns:
                if re.search(p, self.content, re.IGNORECASE):
                    if feature not in self.game_features:
                        self.game_features.append(feature)
                    break

class PatternDetector:
    def __init__(self, security, game):
        self.security = security
        self.game = game
        self.patterns = []

    def detect(self):
        if any(f['category'] == 'INPUT VALIDATION / RATE LIMITS' for f in self.security.findings) and any(f['category'] == 'Unsafe Input' for f in self.security.weaknesses):
            self.patterns.append({
                'name': 'MIXED_INPUT_HANDLING',
                'evidence': 'Validation and unsafe execution functions detected.',
                'confidence': 65
            })
        
        if 'Player Movement' in self.game.game_features and 'Networking/Replication' in self.game.game_features:
            self.patterns.append({
                'name': 'NETWORKED_MOVEMENT_VALIDATION',
                'evidence': 'Movement mechanics with server-side checks.',
                'confidence': 85
            })

class ConfidenceEngine:
    def calculate(self, base_score, evidence_count, is_primary):
        score = base_score
        score += min(evidence_count * 5, 20)
        if is_primary:
            score += 10
        return min(score, 100)

    def get_label(self, score):
        if score >= 90: return 'HIGH'
        if score >= 60: return 'MEDIUM'
        if score >= 30: return 'LOW'
        return 'UNCERTAIN'

class ReportGenerator:
    def __init__(self, source, symbols, control_flow, data_flow, call_graph, dependencies, behavioral, security, game, patterns, confidence_engine, obfuscation=None):
        self.source = source
        self.symbols = symbols
        self.control_flow = control_flow
        self.data_flow = data_flow
        self.call_graph = call_graph
        self.dependencies = dependencies
        self.behavioral = behavioral
        self.security = security
        self.game = game
        self.patterns = patterns
        self.confidence_engine = confidence_engine
        self.obfuscation = obfuscation

    def generate(self):
        lines = []
        lines.append("=" * 70)
        lines.append("COMPREHENSIVE STATIC ANALYSIS REPORT")
        lines.append("=" * 70)
        lines.append("Language: " + self.source.lang.upper())
        lines.append("Generated: " + datetime.utcnow().isoformat())
        lines.append("Lines: " + str(self.source.total_lines) + " | Chars: " + str(self.source.total_chars))
        
        if self.obfuscation:
            is_obs, techniques, conf = self.obfuscation
            lines.append("Obfuscation Detected: " + ("YES" if is_obs else "NO"))
            if is_obs:
                lines.append("Techniques: " + ", ".join(techniques))
                lines.append("Confidence: " + str(conf) + "%")
        
        lines.append("=" * 70)
        lines.append("")

        lines.append("1. ARCHITECTURE OVERVIEW")
        lines.append("-" * 70)
        lines.append("Functions: " + str(len(self.symbols.functions)))
        lines.append("Classes/Structs: " + str(len(self.symbols.classes)))
        lines.append("Imports: " + str(len(self.symbols.imports)))
        lines.append("Decorators: " + str(len(self.symbols.decorators)))
        lines.append("Macros: " + str(len(self.symbols.macros)))
        lines.append("")

        lines.append("2. CONTROL FLOW ANALYSIS")
        lines.append("-" * 70)
        lines.append("Branches: " + str(self.control_flow.branches))
        lines.append("Loops: " + str(self.control_flow.loops))
        lines.append("Early Returns: " + str(self.control_flow.early_returns))
        lines.append("Error Paths: " + str(self.control_flow.error_paths))
        lines.append("Recursive Functions: " + str(len(self.control_flow.recursion)))
        lines.append("")

        lines.append("3. DATA FLOW ANALYSIS")
        lines.append("-" * 70)
        lines.append("Transformations: " + str(self.data_flow.transformations))
        lines.append("")

        lines.append("4. DEPENDENCY ANALYSIS")
        lines.append("-" * 70)
        lines.append("External Libraries: " + (", ".join(self.dependencies.external_libs[:10]) if self.dependencies.external_libs else "None"))
        lines.append("Frameworks: " + (", ".join(self.dependencies.frameworks[:10]) if self.dependencies.frameworks else "None"))
        lines.append("Internal Modules: " + (", ".join(self.dependencies.internal_modules[:10]) if self.dependencies.internal_modules else "None"))
        lines.append("")

        lines.append("5. BEHAVIORAL CLASSIFICATION")
        lines.append("-" * 70)
        for cat, funcs in self.behavioral.categories.items():
            score = self.confidence_engine.calculate(70, len(funcs), True)
            label = self.confidence_engine.get_label(score)
            lines.append("[" + label + "] " + cat + ": " + ", ".join(funcs[:5]))
        if not self.behavioral.categories:
            lines.append("No behavioral patterns detected.")
        lines.append("")

        lines.append("6. SECURITY ANALYSIS")
        lines.append("-" * 70)
        if self.security.findings:
            for finding in self.security.findings:
                score = self.confidence_engine.calculate(80, len(finding['evidence']), True)
                label = self.confidence_engine.get_label(score)
                lines.append("[" + label + "] " + finding['category'])
                lines.append("  Evidence: " + ", ".join(finding['evidence'][:5]))
        else:
            lines.append("No security mechanisms detected.")
        
        if self.security.weaknesses:
            lines.append("")
            lines.append("Potential Weaknesses:")
            for w in self.security.weaknesses:
                score = self.confidence_engine.calculate(60, len(w['evidence']), False)
                label = self.confidence_engine.get_label(score)
                lines.append("  [" + label + "] " + w['category'] + ": " + ", ".join(w['evidence']))
        lines.append("")

        lines.append("7. GAME ANALYSIS")
        lines.append("-" * 70)
        if self.game.game_features:
            for feature in self.game.game_features:
                lines.append("- " + feature)
        else:
            lines.append("No specific game mechanics detected.")
        lines.append("")

        lines.append("8. PATTERN DETECTION")
        lines.append("-" * 70)
        if self.patterns.patterns:
            for p in self.patterns.patterns:
                label = self.confidence_engine.get_label(p['confidence'])
                lines.append("Pattern: " + p['name'])
                lines.append("Confidence: " + label + " (" + str(p['confidence']) + "%)")
                lines.append("Evidence: " + p['evidence'])
                lines.append("")
        else:
            lines.append("No complex semantic patterns detected.")
        lines.append("")

        lines.append("9. LIMITATIONS")
        lines.append("-" * 70)
        lines.append("Analysis is based on static heuristics and AST structure.")
        lines.append("Dynamic behavior and runtime obfuscation are not evaluated.")
        lines.append("Confidence scores reflect structural evidence only.")
        lines.append("=" * 70)
        lines.append("END OF REPORT")
        lines.append("=" * 70)

        return "\n".join(lines)

def build_embed(phase, filename, lang, progress_pct, fields=None, color=0x2F3136):
    embed = discord.Embed(
        title="Static Analysis Pipeline",
        description="Target: " + filename + "\nLanguage: " + lang.upper(),
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Phase " + str(phase) + "/3 - " + str(progress_pct) + "% complete")

    status = ["[ ]", "[ ]", "[ ]"]
    labels = [
        "AST and Symbol Extraction",
        "Control, Data, and Semantic Analysis",
        "Report Generation"
    ]
    for i in range(phase):
        status[i] = "[x]"
    if phase < 3:
        status[phase] = "[...]"

    for i in range(3):
        embed.add_field(
            name="Task " + str(i+1) + " " + status[i],
            value=labels[i],
            inline=False
        )

    if fields:
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)

    return embed

def generate_implementation(analysis_text, target_lang, detected_features, security_findings):
    lines = []
    
    if target_lang in ['.lua', '.luau']:
        lines.append("local RunService = game:GetService(\"RunService\")")
        lines.append("local Players = game:GetService(\"Players\")")
        lines.append("local LocalPlayer = Players.LocalPlayer")
        lines.append("")
        lines.append("local MAX_WALK_SPEED = 16")
        lines.append("local MAX_JUMP_POWER = 50")
        lines.append("")
        lines.append("local function enforce_physics()")
        lines.append("    local character = LocalPlayer.Character")
        lines.append("    if character then")
        lines.append("        local humanoid = character:FindFirstChildOfClass(\"Humanoid\")")
        lines.append("        if humanoid then")
        lines.append("            if humanoid.WalkSpeed > MAX_WALK_SPEED then")
        lines.append("                humanoid.WalkSpeed = MAX_WALK_SPEED")
        lines.append("            end")
        lines.append("            if humanoid.JumpPower > MAX_JUMP_POWER then")
        lines.append("                humanoid.JumpPower = MAX_JUMP_POWER")
        lines.append("            end")
        lines.append("        end")
        lines.append("    end")
        lines.append("end")
        lines.append("")
        lines.append("local function validate_integrity()")
        lines.append("    local core_scripts = game:GetService(\"CoreGui\")")
        lines.append("    if not core_scripts then")
        lines.append("        LocalPlayer:Kick(\"Integrity check failed.\")")
        lines.append("    end")
        lines.append("end")
        lines.append("")
        lines.append("RunService.Heartbeat:Connect(enforce_physics)")
        lines.append("RunService.RenderStepped:Connect(validate_integrity)")
        lines.append("")
        lines.append("print(\"Enforcement initialized.\")")
        
    elif target_lang == '.py':
        lines.append("import time")
        lines.append("import threading")
        lines.append("import hashlib")
        lines.append("")
        lines.append("class VelocityEnforcer:")
        lines.append("    def __init__(self, max_speed=16.0):")
        lines.append("        self.max_speed = max_speed")
        lines.append("        self.running = True")
        lines.append("        self.current_speed = 0.0")
        lines.append("")
        lines.append("    def start(self):")
        lines.append("        self.thread = threading.Thread(target=self._enforcement_loop)")
        lines.append("        self.thread.daemon = True")
        lines.append("        self.thread.start()")
        lines.append("")
        lines.append("    def stop(self):")
        lines.append("        self.running = False")
        lines.append("        if self.thread.is_alive():")
        lines.append("            self.thread.join()")
        lines.append("")
        lines.append("    def set_speed(self, speed):")
        lines.append("        self.current_speed = speed")
        lines.append("")
        lines.append("    def _enforcement_loop(self):")
        lines.append("        while self.running:")
        lines.append("            if self.current_speed > self.max_speed:")
        lines.append("                self.current_speed = self.max_speed")
        lines.append("            time.sleep(0.1)")
        lines.append("")
        lines.append("class IntegrityVerifier:")
        lines.append("    def __init__(self, expected_hash):")
        lines.append("        self.expected_hash = expected_hash")
        lines.append("")
        lines.append("    def verify(self, data):")
        lines.append("        computed = hashlib.sha256(data.encode()).hexdigest()")
        lines.append("        return computed == self.expected_hash")
        lines.append("")
        lines.append("if __name__ == \"__main__\":")
        lines.append("    enforcer = VelocityEnforcer(max_speed=16.0)")
        lines.append("    enforcer.start()")
        lines.append("    enforcer.set_speed(20.0)")
        lines.append("    print(f\"Enforced speed: {enforcer.current_speed}\")")
        lines.append("    enforcer.stop()")
        
    elif target_lang in ['.js', '.ts']:
        lines.append("class VelocityEnforcer {")
        lines.append("    constructor(maxSpeed = 16) {")
        lines.append("        this.maxSpeed = maxSpeed;")
        lines.append("        this.currentSpeed = 0;")
        lines.append("        this.running = true;")
        lines.append("    }")
        lines.append("")
        lines.append("    start() {")
        lines.append("        setInterval(() => this.enforce(), 100);")
        lines.append("    }")
        lines.append("")
        lines.append("    stop() {")
        lines.append("        this.running = false;")
        lines.append("    }")
        lines.append("")
        lines.append("    setSpeed(speed) {")
        lines.append("        this.currentSpeed = speed;")
        lines.append("    }")
        lines.append("")
        lines.append("    enforce() {")
        lines.append("        if (this.currentSpeed > this.maxSpeed) {")
        lines.append("            this.currentSpeed = this.maxSpeed;")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("class IntegrityVerifier {")
        lines.append("    constructor(expectedHash) {")
        lines.append("        this.expectedHash = expectedHash;")
        lines.append("    }")
        lines.append("")
        lines.append("    async verify(data) {")
        lines.append("        const encoder = new TextEncoder();")
        lines.append("        const dataBuffer = encoder.encode(data);")
        lines.append("        const hashBuffer = await crypto.subtle.digest('SHA-256', dataBuffer);")
        lines.append("        const hashArray = Array.from(new Uint8Array(hashBuffer));")
        lines.append("        const hashHex = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');")
        lines.append("        return hashHex === this.expectedHash;")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("const enforcer = new VelocityEnforcer(16);")
        lines.append("enforcer.start();")
        lines.append("enforcer.setSpeed(20);")
        lines.append("console.log(`Enforced speed: ${enforcer.currentSpeed}`);")
        
    elif target_lang == '.cs':
        lines.append("using System;")
        lines.append("using System.Threading;")
        lines.append("using System.Security.Cryptography;")
        lines.append("using System.Text;")
        lines.append("")
        lines.append("public class VelocityEnforcer")
        lines.append("{")
        lines.append("    private double maxSpeed;")
        lines.append("    private double currentSpeed;")
        lines.append("    private bool running;")
        lines.append("")
        lines.append("    public VelocityEnforcer(double max = 16.0)")
        lines.append("    {")
        lines.append("        maxSpeed = max;")
        lines.append("        currentSpeed = 0.0;")
        lines.append("        running = true;")
        lines.append("    }")
        lines.append("")
        lines.append("    public void Start()")
        lines.append("    {")
        lines.append("        Thread thread = new Thread(EnforcementLoop);")
        lines.append("        thread.IsBackground = true;")
        lines.append("        thread.Start();")
        lines.append("    }")
        lines.append("")
        lines.append("    public void Stop()")
        lines.append("    {")
        lines.append("        running = false;")
        lines.append("    }")
        lines.append("")
        lines.append("    public void SetSpeed(double speed)")
        lines.append("    {")
        lines.append("        currentSpeed = speed;")
        lines.append("    }")
        lines.append("")
        lines.append("    private void EnforcementLoop()")
        lines.append("    {")
        lines.append("        while (running)")
        lines.append("        {")
        lines.append("            if (currentSpeed > maxSpeed)")
        lines.append("                currentSpeed = maxSpeed;")
        lines.append("            Thread.Sleep(100);")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("public class IntegrityVerifier")
        lines.append("{")
        lines.append("    private string expectedHash;")
        lines.append("")
        lines.append("    public IntegrityVerifier(string hash)")
        lines.append("    {")
        lines.append("        expectedHash = hash;")
        lines.append("    }")
        lines.append("")
        lines.append("    public bool Verify(string data)")
        lines.append("    {")
        lines.append("        using (SHA256 sha256 = SHA256.Create())")
        lines.append("        {")
        lines.append("            byte[] bytes = Encoding.UTF8.GetBytes(data);")
        lines.append("            byte[] hash = sha256.ComputeHash(bytes);")
        lines.append("            string computedHash = BitConverter.ToString(hash).Replace(\"-\", \"\").ToLower();")
        lines.append("            return computedHash == expectedHash;")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        
    elif target_lang == '.java':
        lines.append("import java.security.MessageDigest;")
        lines.append("import java.security.NoSuchAlgorithmException;")
        lines.append("")
        lines.append("public class VelocityEnforcer {")
        lines.append("    private double maxSpeed;")
        lines.append("    private double currentSpeed;")
        lines.append("    private boolean running;")
        lines.append("")
        lines.append("    public VelocityEnforcer(double max) {")
        lines.append("        this.maxSpeed = max;")
        lines.append("        this.currentSpeed = 0.0;")
        lines.append("        this.running = true;")
        lines.append("    }")
        lines.append("")
        lines.append("    public void start() {")
        lines.append("        Thread thread = new Thread(this::enforcementLoop);")
        lines.append("        thread.setDaemon(true);")
        lines.append("        thread.start();")
        lines.append("    }")
        lines.append("")
        lines.append("    public void stop() {")
        lines.append("        running = false;")
        lines.append("    }")
        lines.append("")
        lines.append("    public void setSpeed(double speed) {")
        lines.append("        currentSpeed = speed;")
        lines.append("    }")
        lines.append("")
        lines.append("    private void enforcementLoop() {")
        lines.append("        while (running) {")
        lines.append("            if (currentSpeed > maxSpeed) {")
        lines.append("                currentSpeed = maxSpeed;")
        lines.append("            }")
        lines.append("            try {")
        lines.append("                Thread.sleep(100);")
        lines.append("            } catch (InterruptedException e) {")
        lines.append("                Thread.currentThread().interrupt();")
        lines.append("            }")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("class IntegrityVerifier {")
        lines.append("    private String expectedHash;")
        lines.append("")
        lines.append("    public IntegrityVerifier(String hash) {")
        lines.append("        this.expectedHash = hash;")
        lines.append("    }")
        lines.append("")
        lines.append("    public boolean verify(String data) throws NoSuchAlgorithmException {")
        lines.append("        MessageDigest sha256 = MessageDigest.getInstance(\"SHA-256\");")
        lines.append("        byte[] hash = sha256.digest(data.getBytes());")
        lines.append("        StringBuilder hexString = new StringBuilder();")
        lines.append("        for (byte b : hash) {")
        lines.append("            String hex = Integer.toHexString(0xff & b);")
        lines.append("            if (hex.length() == 1) hexString.append('0');")
        lines.append("            hexString.append(hex);")
        lines.append("        }")
        lines.append("        return hexString.toString().equals(expectedHash);")
        lines.append("    }")
        lines.append("}")
        
    elif target_lang == '.rs':
        lines.append("use std::thread;")
        lines.append("use std::time::Duration;")
        lines.append("use sha2::{Sha256, Digest};")
        lines.append("")
        lines.append("pub struct VelocityEnforcer {")
        lines.append("    max_speed: f64,")
        lines.append("    current_speed: f64,")
        lines.append("    running: bool,")
        lines.append("}")
        lines.append("")
        lines.append("impl VelocityEnforcer {")
        lines.append("    pub fn new(max: f64) -> Self {")
        lines.append("        VelocityEnforcer {")
        lines.append("            max_speed: max,")
        lines.append("            current_speed: 0.0,")
        lines.append("            running: true,")
        lines.append("        }")
        lines.append("    }")
        lines.append("")
        lines.append("    pub fn start(&mut self) {")
        lines.append("        let mut enforcer = self.clone();")
        lines.append("        thread::spawn(move || {")
        lines.append("            enforcer.enforcement_loop();")
        lines.append("        });")
        lines.append("    }")
        lines.append("")
        lines.append("    pub fn stop(&mut self) {")
        lines.append("        self.running = false;")
        lines.append("    }")
        lines.append("")
        lines.append("    pub fn set_speed(&mut self, speed: f64) {")
        lines.append("        self.current_speed = speed;")
        lines.append("    }")
        lines.append("")
        lines.append("    fn enforcement_loop(&mut self) {")
        lines.append("        while self.running {")
        lines.append("            if self.current_speed > self.max_speed {")
        lines.append("                self.current_speed = self.max_speed;")
        lines.append("            }")
        lines.append("            thread::sleep(Duration::from_millis(100));")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("pub struct IntegrityVerifier {")
        lines.append("    expected_hash: String,")
        lines.append("}")
        lines.append("")
        lines.append("impl IntegrityVerifier {")
        lines.append("    pub fn new(hash: String) -> Self {")
        lines.append("        IntegrityVerifier { expected_hash: hash }")
        lines.append("    }")
        lines.append("")
        lines.append("    pub fn verify(&self, data: &str) -> bool {")
        lines.append("        let mut hasher = Sha256::new();")
        lines.append("        hasher.update(data.as_bytes());")
        lines.append("        let result = hasher.finalize();")
        lines.append("        let computed_hash = format!(\"{:x}\", result);")
        lines.append("        computed_hash == self.expected_hash")
        lines.append("    }")
        lines.append("}")
        
    else:
        lines.append("#include <iostream>")
        lines.append("#include <thread>")
        lines.append("#include <chrono>")
        lines.append("#include <string>")
        lines.append("")
        lines.append("class VelocityEnforcer {")
        lines.append("private:")
        lines.append("    double max_speed;")
        lines.append("    double current_speed;")
        lines.append("    bool running;")
        lines.append("")
        lines.append("public:")
        lines.append("    VelocityEnforcer(double max) : max_speed(max), current_speed(0.0), running(true) {}")
        lines.append("")
        lines.append("    void start() {")
        lines.append("        std::thread t(&VelocityEnforcer::enforcement_loop, this);")
        lines.append("        t.detach();")
        lines.append("    }")
        lines.append("")
        lines.append("    void stop() {")
        lines.append("        running = false;")
        lines.append("    }")
        lines.append("")
        lines.append("    void set_speed(double speed) {")
        lines.append("        current_speed = speed;")
        lines.append("    }")
        lines.append("")
        lines.append("    void enforcement_loop() {")
        lines.append("        while (running) {")
        lines.append("            if (current_speed > max_speed) {")
        lines.append("                current_speed = max_speed;")
        lines.append("            }")
        lines.append("            std::this_thread::sleep_for(std::chrono::milliseconds(100));")
        lines.append("        }")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        lines.append("int main() {")
        lines.append("    VelocityEnforcer enforcer(16.0);")
        lines.append("    enforcer.start();")
        lines.append("    enforcer.set_speed(20.0);")
        lines.append("    std::cout << \"Enforced speed: \" << enforcer.current_speed << std::endl;")
        lines.append("    enforcer.stop();")
        lines.append("    return 0;")
        lines.append("}")

    return "\n".join(lines)

_TREE_SYNCED = False

@bot.event
async def on_ready():
    global _TREE_SYNCED
    print("BOT IS ONLINE")
    print("Bot Name:", bot.user.name)
    print("Bot ID:", bot.user.id)
    print("Servers:", len(bot.guilds))
    print("Intents configured")
    if not _TREE_SYNCED:
        try:
            await bot.tree.sync()
            _TREE_SYNCED = True
            print("Slash commands synced")
        except Exception as exc:
            print("SLASH SYNC ERROR:", repr(exc))

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, DuplicateCommandEvent)):
        return
    print("COMMAND ERROR:", error)
    print(traceback.format_exc())
    await ctx.send("Error: " + str(error))

@bot.command(name='ping')
async def ping(ctx):
    await ctx.send("Pong! Bot is working.")

@bot.command(name='help')
async def help_command(ctx):
    embed = discord.Embed(
        title="📚 Bot Commands Help",
        description="Complete guide to using this bot",
        color=0x3498db,
        timestamp=datetime.utcnow()
    )
    
    embed.add_field(
        name=".get <URL>",
        value="Fetches a file from a URL and analyzes it for obfuscation.\n**Example:** `.get https://raw.githubusercontent.com/user/repo/file.lua`",
        inline=False
    )
    
    embed.add_field(
        name=".re",
        value="Reverse engineers an attached code file. Upload a file and use this command to get a comprehensive analysis.\n**Supports:** Python, JavaScript, Lua, C++, Java, Rust, and more",
        inline=False
    )
    
    embed.add_field(
        name=".bypass",
        value="Generates an implementation/enforcement script based on the analysis report from `.re` command.\n**Attach the .txt analysis file**",
        inline=False
    )
    
    embed.add_field(
        name=".obfuscate",
        value="Creates a private channel for secure code obfuscation. Upload your file, get it obfuscated via DM, and the channel auto-deletes.\n**Now with AST-based scope-aware obfuscation!**",
        inline=False
    )
    
    embed.add_field(
        name=".deobf",
        value="Deobfuscates an obfuscated code file. Attach the obfuscated file and the bot will attempt to reverse the obfuscation.\n**Supports Prometheus, MoonSec, and more**",
        inline=False
    )
    
    embed.add_field(
        name="/api",
        value="Configure a private OpenAI-compatible API in DMs. The API key is kept in memory only. Use /api-clear to remove it.",
        inline=False
    )
    embed.add_field(
        name=".cleanup [1-500]",
        value="Deletes recent messages sent by this bot in the current channel. Requires Manage Messages.",
        inline=False
    )
    embed.add_field(
        name=".ping",
        value="Checks if the bot is online and responsive.",
        inline=False
    )
    
    embed.set_footer(text="Use these commands to analyze, obfuscate, and protect your code")
    
    try:
        await ctx.author.send(embed=embed)
        await ctx.send("✅ Help sent to your DMs!")
    except discord.Forbidden:
        await ctx.send("❌ I couldn't send you a DM. Please enable DMs from server members.")

@bot.command(name='get')
async def fetch_file(ctx, url=None):
    if not url:
        await ctx.send("Usage: .get <URL>")
        return

    try:
        parsed_url = urlparse(url)
        if not parsed_url.scheme or not parsed_url.netloc:
            await ctx.send("Invalid URL format. Please provide a complete URL (e.g., https://example.com/file.lua)")
            return

        await ctx.send("Fetching file from: " + url)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    await ctx.send("Failed to fetch file. Status code: " + str(response.status))
                    return

                content = await response.text()
                
                if not content or len(content.strip()) == 0:
                    await ctx.send("The file is empty.")
                    return

                filename_match = re.search(r'/([^/]+\.(?:lua|py|js|ts|c|cpp|h|hpp|cs|java|rs|php|m|swift|go|rb|kt|sh|ps1|r|pl|ex|erl|hs|ml|zig|nim|v|d|sol))$', url)
                if filename_match:
                    filename = filename_match.group(1)
                else:
                    ext_match = re.search(r'\.([a-z0-9]+)(?:\?.*)?$', url)
                    if ext_match:
                        ext = "." + ext_match.group(1)
                        filename = "fetched_file" + ext
                    else:
                        filename = "fetched_file.txt"

                ext = os.path.splitext(filename)[1].lower()
                lang = LANG_MAP.get(ext, 'unknown')

                detector = ObfuscationDetector(content)
                is_obfuscated, techniques, confidence = detector.detect()

                embed = discord.Embed(
                    title="File Analysis",
                    description="**File:** " + filename + "\n**Language:** " + lang.upper() + "\n**Size:** " + str(len(content)) + " bytes",
                    color=0x2ECC71 if not is_obfuscated else 0xE74C3C
                )
                embed.add_field(name="Obfuscation Detected", value="**YES**" if is_obfuscated else "**NO**", inline=True)
                embed.add_field(name="Confidence", value=str(confidence) + "%", inline=True)
                embed.add_field(name="Lines", value=str(len(content.splitlines())), inline=True)

                if is_obfuscated:
                    techniques_str = "\n".join(["• " + t for t in techniques])
                    embed.add_field(name="Techniques Found", value=techniques_str[:1024], inline=False)
                    embed.add_field(name="Warning", value="This file appears to be obfuscated. Analysis may be limited.", inline=False)

                await ctx.send(embed=embed)

                file_path = "temp_" + filename
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)

                await ctx.send(file=discord.File(file_path, filename=filename))
                os.remove(file_path)

    except aiohttp.ClientError as e:
        print("HTTP ERROR:", e)
        await ctx.send("Failed to connect to the URL. Error: " + str(e))
    except Exception as e:
        print("FETCH ERROR:", e)
        print(traceback.format_exc())
        await ctx.send("Error fetching file: " + str(e))

@bot.command(name='obfuscate')
async def obfuscate_command(ctx):
    owner_id = ctx.guild.owner.id
    
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        ctx.guild.get_member(owner_id): discord.PermissionOverwrite(read_messages=True, send_messages=True) if ctx.guild.get_member(owner_id) else discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    
    channel_name = "obfuscate-" + ctx.author.name
    channel = await ctx.guild.create_text_channel(channel_name, overwrites=overwrites, reason="Private obfuscation channel")
    
    embed = discord.Embed(
        title="🔒 Private Obfuscation Channel",
        description="This is a private channel for code obfuscation.\n\n**Instructions:**\n1. Upload your code file\n2. The bot will obfuscate it using AST-based scope-aware transformation\n3. You'll receive the obfuscated file via DM\n4. This channel will be deleted automatically",
        color=0x9B59B6
    )
    await channel.send(embed=embed)
    await channel.send("📎 **Upload your file now!**")
    
    await ctx.send(f"✅ Private channel created: {channel.mention}")
    
    def check(m):
        return m.channel == channel and m.author == ctx.author and len(m.attachments) > 0
    
    try:
        msg = await bot.wait_for('message', check=check, timeout=300.0)
        
        attachment = msg.attachments[0]
        filename = attachment.filename
        ext = os.path.splitext(filename)[1].lower()
        
        if ext not in LANG_MAP:
            await channel.send("❌ Unsupported file type. Supported: .lua, .luau, .py, .js, .ts, .c, .cpp, .cs, .java, .rs")
            await asyncio.sleep(3)
            await channel.delete()
            return
        
        file_data = await attachment.read()
        content = file_data.decode('utf-8', errors='ignore')
        
        await channel.send(" Obfuscating your code with AST-based scope-aware engine...")
        
        lang = LANG_MAP.get(ext, 'unknown')
        
        if lang in ['lua', 'luau']:
            obfuscator = AdvancedLuaObfuscatorEngine(content)
            obfuscated_code = obfuscator.obfuscate()
        else:
            # Preserve the existing command/API, but do not run a Lua
            # transformer over non-Lua source files.
            await channel.send("❌ Advanced obfuscation is currently available for Lua/Luau only.")
            await asyncio.sleep(2)
            await channel.delete()
            return
        
        obs_filename = "obfuscated_" + filename
        obs_path = "temp_" + obs_filename
        
        with open(obs_path, 'w', encoding='utf-8') as f:
            f.write(obfuscated_code)
        
        try:
            with open(obs_path, 'rb') as f:
                await ctx.author.send("✅ **Your obfuscated file is ready!**\n\n⚠️ **Warning:** Keep this file secure. Obfuscation makes code harder to read but not impossible to reverse.\n\n**Obfuscation Type:** AST-based scope-aware transformation", file=discord.File(f, filename=obs_filename))
        except discord.Forbidden:
            await channel.send("❌ I couldn't send you a DM. Please enable DMs from server members.")
            await asyncio.sleep(3)
            await channel.delete()
            os.remove(obs_path)
            return
        
        await channel.send("✅ Obfuscation complete! Check your DMs.\n🗑️ Deleting channel in 5 seconds...")
        await asyncio.sleep(5)
        
        os.remove(obs_path)
        await channel.delete()
        
    except asyncio.TimeoutError:
        await channel.send("⏰ Timeout! No file uploaded. Deleting channel...")
        await asyncio.sleep(3)
        await channel.delete()
    except Exception as e:
        print("OBFUSCATE ERROR:", e)
        print(traceback.format_exc())
        await channel.send("❌ An error occurred: " + str(e))
        await asyncio.sleep(3)
        await channel.delete()

@bot.command(name='deobf')
async def deobfuscate_command(ctx):
    if not ctx.message.attachments:
        await ctx.send("❌ Please attach an obfuscated file to deobfuscate.")
        return
    
    attachment = ctx.message.attachments[0]
    filename = attachment.filename
    ext = os.path.splitext(filename)[1].lower()
    
    if ext not in LANG_MAP:
        await ctx.send(" Unsupported file type. Supported: .lua, .luau, .py, .js, .ts")
        return
    
    try:
        file_data = await attachment.read()
        content = file_data.decode('utf-8', errors='ignore')
        
        await ctx.send("🔄 Analyzing and deobfuscating with modular engine...")
        
        lang = LANG_MAP.get(ext, 'unknown')
        
        if lang in ['lua', 'luau']:
            deob_engine = AdvancedDeobfuscatorEngine()
            deobfuscated_code, meta = deob_engine.transform(content)

            deobs_filename = "deobfuscated_" + filename
            deobs_path = "temp_" + deobs_filename
            
            with open(deobs_path, 'w', encoding='utf-8') as f:
                f.write(deobfuscated_code)
            
            embed = discord.Embed(
                title="✅ Deobfuscation Complete",
                description="**Original:** " + filename + "\n**Language:** " + lang.upper() + "\n**Detected Engine:** " + meta['engine'] + "\n**Confidence:** " + f"{meta['confidence']:.0%}" + "\n**Size:** " + str(len(deobfuscated_code)) + " bytes",
                color=0x2ECC71
            )
            
            if meta.get('signatures'):
                embed.add_field(name="Signatures Found", value="\n".join(meta['signatures']), inline=False)
            
            await ctx.send(embed=embed, file=discord.File(deobs_path, filename=deobs_filename))
            os.remove(deobs_path)
        else:
            await ctx.send("❌ Deobfuscation for " + lang + " is not yet implemented.")
        
    except Exception as e:
        print("DEOBF ERROR:", e)
        print(traceback.format_exc())
        await ctx.send("❌ Error deobfuscating: " + str(e))

@bot.command(name='re')
async def reverse_engineer(ctx):
    print("RE command received from", ctx.author)
    
    if not ctx.message.attachments:
        await ctx.send("No file attached. Please upload a code file.")
        return

    attachment = ctx.message.attachments[0]
    filename = _safe_filename(attachment.filename)
    ext = os.path.splitext(filename)[1].lower()
    lang = LANG_MAP.get(ext, 'unknown')

    print("Processing file:", filename, "Extension:", ext, "Language:", lang)

    if lang == 'unknown':
        await ctx.send("Unsupported file type: " + ext)
        return

    try:
        file_path = os.path.join("tmp", f"{uuid.uuid4().hex}_{filename}")
        os.makedirs("tmp", exist_ok=True)
        await attachment.save(file_path)
        print("File saved:", file_path)

        with open(file_path, 'r', errors='ignore') as f:
            content = f.read()
        print("File read, length:", len(content))

        msg = await ctx.send(embed=build_embed(0, filename, lang, 0, color=0xF1C40F))

        await asyncio.sleep(1)
        await msg.edit(embed=build_embed(1, filename, lang, 33, color=0xF39C12))

        detector = ObfuscationDetector(content)
        is_obfuscated, techniques, confidence = detector.detect()

        source = SourceAnalyzer(content, ext)
        ast_builder = ASTBuilder(content, lang)
        symbols = SymbolExtractor(ast_builder.get_root(), content)
        symbols.extract()
        
        control_flow = ControlFlowAnalyzer(content)
        control_flow.analyze(symbols.functions)
        
        data_flow = DataFlowAnalyzer(content)
        data_flow.analyze()
        
        call_graph = CallGraphBuilder(symbols.functions, content)
        call_graph.build()
        
        dependencies = DependencyAnalyzer(symbols.imports)
        dependencies.analyze()
        
        behavioral = BehavioralClassifier(symbols.functions, content)
        behavioral.classify()
        
        security = SecurityAnalyzer(content)
        security.analyze()
        
        game = GameAnalyzer(content)
        game.analyze()
        
        patterns = PatternDetector(security, game)
        patterns.detect()
        
        confidence_engine = ConfidenceEngine()

        await asyncio.sleep(1)

        phase2_fields = [
            ("Functions", "`" + str(len(symbols.functions)) + "`"),
            ("Classes", "`" + str(len(symbols.classes)) + "`"),
            ("Control Branches", "`" + str(control_flow.branches) + "`"),
            ("Security Findings", "`" + str(len(security.findings)) + "`"),
            ("Game Features", "`" + str(len(game.game_features)) + "`"),
            ("Obfuscated", "`" + ("YES" if is_obfuscated else "NO") + "`")
        ]
        await msg.edit(embed=build_embed(2, filename, lang, 66, fields=phase2_fields, color=0xE67E22))

        await asyncio.sleep(1)

        report_gen = ReportGenerator(source, symbols, control_flow, data_flow, call_graph, dependencies, behavioral, security, game, patterns, confidence_engine, (is_obfuscated, techniques, confidence) if is_obfuscated else None)
        report_content = report_gen.generate()
        
        out_path = "analysis_" + filename + ".txt"
        with open(out_path, 'w') as f:
            f.write(report_content)
        
        print("Report generated:", out_path)

        phase3_fields = [
            ("Patterns Detected", "`" + str(len(patterns.patterns)) + "`"),
            ("Weaknesses", "`" + str(len(security.weaknesses)) + "`"),
            ("Output File", "`" + out_path + "`"),
            ("Status", "`COMPLETE`")
        ]
        await msg.edit(embed=build_embed(3, filename, lang, 100, fields=phase3_fields, color=0x2ECC71))

        await ctx.send("Analysis complete.", file=discord.File(out_path))
        
        os.remove(file_path)
        os.remove(out_path)
        print("Cleanup done")

    except Exception as e:
        print("ERROR:", e)
        print(traceback.format_exc())
        await ctx.send("Error processing file: " + str(e))

@bot.command(name='bypass')
async def generate_bypass(ctx):
    if not ctx.message.attachments:
        await ctx.send("Attach the analysis file from the .re command.")
        return

    attachment = ctx.message.attachments[0]
    filename = _safe_filename(attachment.filename, "analysis.txt")
    
    if not filename.lower().endswith('.txt'):
        await ctx.send("The file must be a .txt analysis report.")
        return

    try:
        file_path = os.path.join("tmp", f"{uuid.uuid4().hex}_{filename}")
        os.makedirs("tmp", exist_ok=True)
        await attachment.save(file_path)

        with open(file_path, 'r', errors='ignore') as f:
            content = f.read()

        lang_match = re.search(r'Language:\s*(\w+)', content)
        ext = '.lua'
        if lang_match:
            lang = lang_match.group(1).lower()
            if lang in REVERSE_LANG_MAP:
                ext = REVERSE_LANG_MAP[lang]

        features = re.findall(r'- (Player Movement|Networking/Replication|Damage/Health|Inventory|Anti-Cheat)', content)
        findings = re.findall(r'\[(HIGH|MEDIUM|LOW)\] (.*?)(?:\n|$)', content)

        implementation = generate_implementation(content, ext, features, findings)
        
        out_path = os.path.join("tmp", f"implementation_{uuid.uuid4().hex}{ext}")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(implementation)

        await ctx.send("Implementation generated.", file=discord.File(out_path))
        os.remove(file_path)
        os.remove(out_path)

    except Exception as e:
        print("BYPASS ERROR:", e)
        print(traceback.format_exc())
        await ctx.send("Error: " + str(e))


@bot.tree.command(name='cleanup', description='Delete recent messages sent by this bot in the current channel')
@app_commands.describe(limit='Maximum number of recent messages to scan (1-500)')
async def cleanup_slash(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 500] = 100):
    if interaction.guild is None:
        await interaction.response.send_message('❌ This command is for server channels.')
        return
    perms = interaction.user.guild_permissions
    if not perms.manage_messages:
        await interaction.response.send_message('❌ You need Manage Messages to use this command.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = 0
    async for msg in interaction.channel.history(limit=int(limit) + 1):
        if msg.author.id == bot.user.id:
            try:
                await msg.delete()
                deleted += 1
            except discord.HTTPException:
                pass
    await interaction.followup.send(f'🧹 Deleted {deleted} recent bot messages.', ephemeral=True)

@bot.command(name='cleanup')
@commands.has_permissions(manage_messages=True)
async def cleanup_command(ctx, limit: int = 100):
    """Delete recent bot messages in the current channel. Use a bounded limit."""
    limit = max(1, min(limit, 500))
    deleted = 0
    async for msg in ctx.channel.history(limit=limit + 1):
        if msg.author.id == bot.user.id:
            try:
                await msg.delete()
                deleted += 1
            except discord.HTTPException:
                pass
    # Avoid creating another message after cleanup.
    print(f'CLEANUP: deleted {deleted} bot messages in channel {ctx.channel.id}')

@cleanup_command.error
async def cleanup_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ You need Manage Messages to use `.cleanup`.')
    elif isinstance(error, commands.BadArgument):
        await ctx.send('Usage: `.cleanup [1-500]`')
    else:
        print('CLEANUP ERROR:', repr(error))


TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("ERROR: No TOKEN found in environment variables")
else:
    # Prevent two local bot processes from using the same token simultaneously.
    # This is the common cause of commands appearing to execute twice.
    LOCK_FILE = os.path.join("tmp", "bot_instance.lock")
    os.makedirs("tmp", exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
        os.close(lock_fd)
        lock_fd = None

        def _release_lock():
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass

        atexit.register(_release_lock)

        print("Starting bot with token...")
        bot.run(TOKEN)
    except FileExistsError:
        print("ERROR: Another bot instance is already running. Stop the old process before starting a new one.")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
