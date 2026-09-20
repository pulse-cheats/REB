import discord
import os
import re
import asyncio
import traceback
import aiohttp
from discord.ext import commands
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse

try:
    from tree_sitter_languages import get_parser
    TREE_SITTER = True
except ImportError:
    TREE_SITTER = False

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)

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
        if TREE_SITTER and lang != 'unknown':
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

@bot.event
async def on_ready():
    print("BOT IS ONLINE")
    print("Bot Name:", bot.user.name)
    print("Bot ID:", bot.user.id)
    print("Servers:", len(bot.guilds))
    print("Intents configured")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print("COMMAND ERROR:", error)
    print(traceback.format_exc())
    await ctx.send("Error: " + str(error))

@bot.command(name='ping')
async def ping(ctx):
    await ctx.send("Pong! Bot is working.")

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

@bot.command(name='re')
async def reverse_engineer(ctx):
    print("RE command received from", ctx.author)
    
    if not ctx.message.attachments:
        await ctx.send("No file attached. Please upload a code file.")
        return

    attachment = ctx.message.attachments[0]
    filename = attachment.filename
    ext = os.path.splitext(filename)[1].lower()
    lang = LANG_MAP.get(ext, 'unknown')

    print("Processing file:", filename, "Extension:", ext, "Language:", lang)

    if lang == 'unknown':
        await ctx.send("Unsupported file type: " + ext)
        return

    try:
        file_path = "temp_" + filename
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
    filename = attachment.filename
    
    if not filename.endswith('.txt'):
        await ctx.send("The file must be a .txt analysis report.")
        return

    try:
        file_path = "temp_" + filename
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
        
        out_path = "implementation" + ext
        with open(out_path, 'w') as f:
            f.write(implementation)

        await ctx.send("Implementation generated.", file=discord.File(out_path))
        os.remove(file_path)
        os.remove(out_path)

    except Exception as e:
        print("BYPASS ERROR:", e)
        print(traceback.format_exc())
        await ctx.send("Error: " + str(e))

TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("ERROR: No TOKEN found in environment variables")
else:
    print("Starting bot with token...")
    bot.run(TOKEN)
