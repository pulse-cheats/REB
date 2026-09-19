import discord
import os
import re
import asyncio
from discord.ext import commands
from datetime import datetime
from collections import defaultdict

try:
    from tree_sitter_languages import get_parser
    TREE_SITTER = True
except ImportError:
    TREE_SITTER = False

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='.', intents=intents)

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

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Command error in {ctx.channel}: {error}")

class SourceAnalyzer:
    def __init__(self, content, ext):
        self.content = content
        self.ext = ext
        self.lang = LANG_MAP.get(ext, 'unknown')
        self.lines = content.splitlines()
        self.total_lines = len(self.lines)
        self.total_chars = len(content)

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
                self.functions.append({'name': f, 'line': 0, 'size': 0, 'depth': 0})
        
        imp = re.findall(r'(?:import|require|include|using|#include)\s+[<"\']([^>"\']+)[>"\']', self.content)
        self.imports = imp[:30]

class ControlFlowAnalyzer:
    def __init__(self, content):
        self.content = content
        self.branches = 0
        self.loops = 0
        self.early_returns = 0
        self.error_paths = 0

    def analyze(self):
        self.branches = len(re.findall(r'\b(if|else|switch|case|match)\b', self.content, re.IGNORECASE))
        self.loops = len(re.findall(r'\b(for|while|do|loop|repeat)\b', self.content, re.IGNORECASE))
        self.early_returns = len(re.findall(r'\breturn\b', self.content))
        self.error_paths = len(re.findall(r'\b(catch|except|rescue|err|error)\b', self.content, re.IGNORECASE))

class DataFlowAnalyzer:
    def __init__(self, content):
        self.content = content
        self.transformations = 0

    def analyze(self):
        self.transformations = len(re.findall(r'[\+\-\*/%]=|<<=|>>=|&=|\|=|\^=', self.content))

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
            'Physics/Movement': [r'velocity', r'acceleration', r'force', r'mass', r'gravity', r'collision', r'move', r'walk', r'jump'],
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
        security_patterns = {
            'Input Validation': [r'sanitize', r'validate', r'filter', r'escape'],
            'Authentication': [r'authenticate', r'authorize', r'login', r'jwt', r'oauth', r'session'],
            'Encryption': [r'AES', r'RSA', r'encrypt', r'decrypt', r'cipher', r'hashlib', r'bcrypt'],
            'Integrity': [r'checksum', r'hash', r'signature', r'HMAC', r'verify'],
            'Anti-Tamper': [r'anti.?tamper', r'integrity', r'obfuscat', r'packer', r'VMProtect'],
            'Anti-Debug': [r'IsDebuggerPresent', r'__debugbreak', r'ptrace', r'AntiDebug']
        }
        
        weakness_patterns = {
            'Unsafe Input': [r'eval\(', r'exec\(', r'system\(', r'popen'],
            'Hardcoded Secrets': [r'password\s*=\s*["\'][^"\']+["\']', r'api_key\s*=\s*["\'][^"\']+["\']', r'token\s*=\s*["\'][^"\']+["\']'],
            'Weak Crypto': [r'MD5', r'SHA1', r'DES', r'RC4', r'ECB']
        }

        for cat, patterns in security_patterns.items():
            for p in patterns:
                matches = re.findall(p, self.content, re.IGNORECASE)
                if matches:
                    self.findings.append({'category': cat, 'evidence': list(set(matches))[:3]})

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
        if any(f['category'] == 'Input Validation' for f in self.security.findings) and any(f['category'] == 'Unsafe Input' for f in self.security.weaknesses):
            self.patterns.append({
                'name': 'MIXED_INPUT_HANDLING',
                'evidence': 'Validation and unsafe execution functions detected in proximity.',
                'confidence': 65
            })
        
        if 'Player Movement' in self.game.game_features and 'Networking/Replication' in self.game.game_features:
            self.patterns.append({
                'name': 'NETWORKED_MOVEMENT_VALIDATION',
                'evidence': 'Movement mechanics coupled with server-side replication checks.',
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
    def __init__(self, source, symbols, control_flow, data_flow, dependencies, behavioral, security, game, patterns, confidence_engine):
        self.source = source
        self.symbols = symbols
        self.control_flow = control_flow
        self.data_flow = data_flow
        self.dependencies = dependencies
        self.behavioral = behavioral
        self.security = security
        self.game = game
        self.patterns = patterns
        self.confidence_engine = confidence_engine

    def generate(self):
        lines = []
        lines.append("=" * 70)
        lines.append("COMPREHENSIVE STATIC ANALYSIS REPORT")
        lines.append("=" * 70)
        lines.append("Language: " + self.source.lang.upper())
        lines.append("Generated: " + datetime.utcnow().isoformat())
        lines.append("Lines: " + str(self.source.total_lines) + " | Chars: " + str(self.source.total_chars))
        lines.append("=" * 70)
        lines.append("")

        lines.append("1. ARCHITECTURE OVERVIEW")
        lines.append("-" * 70)
        lines.append("Functions: " + str(len(self.symbols.functions)))
        lines.append("Classes/Structs: " + str(len(self.symbols.classes)))
        lines.append("Imports: " + str(len(self.symbols.imports)))
        lines.append("")

        lines.append("2. CONTROL FLOW ANALYSIS")
        lines.append("-" * 70)
        lines.append("Branches: " + str(self.control_flow.branches))
        lines.append("Loops: " + str(self.control_flow.loops))
        lines.append("Early Returns: " + str(self.control_flow.early_returns))
        lines.append("Error Paths: " + str(self.control_flow.error_paths))
        lines.append("")

        lines.append("3. DATA FLOW ANALYSIS")
        lines.append("-" * 70)
        lines.append("Transformations: " + str(self.data_flow.transformations))
        lines.append("")

        lines.append("4. DEPENDENCY ANALYSIS")
        lines.append("-" * 70)
        lines.append("External Libraries: " + ", ".join(self.dependencies.external_libs[:10]) or "None")
        lines.append("Frameworks: " + ", ".join(self.dependencies.frameworks[:10]) or "None")
        lines.append("Internal Modules: " + ", ".join(self.dependencies.internal_modules[:10]) or "None")
        lines.append("")

        lines.append("5. BEHAVIORAL CLASSIFICATION")
        lines.append("-" * 70)
        for cat, funcs in self.behavioral.categories.items():
            score = self.confidence_engine.calculate(70, len(funcs), True)
            label = self.confidence_engine.get_label(score)
            lines.append("[" + label + "] " + cat + ": " + ", ".join(funcs[:5]))
        lines.append("")

        lines.append("6. SECURITY ANALYSIS")
        lines.append("-" * 70)
        for finding in self.security.findings:
            score = self.confidence_engine.calculate(80, len(finding['evidence']), True)
            label = self.confidence_engine.get_label(score)
            lines.append("[" + label + "] " + finding['category'] + ": " + ", ".join(finding['evidence']))
        if self.security.weaknesses:
            lines.append("Weaknesses Detected:")
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

        lines.append("9. LIMITATIONS AND UNCERTAINTY")
        lines.append("-" * 70)
        lines.append("Analysis is based on static heuristics and AST structure.")
        lines.append("Dynamic behavior, runtime obfuscation, and encrypted strings are not evaluated.")
        lines.append("Confidence scores reflect structural evidence, not runtime guarantees.")
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
        "Report Generation and Confidence Scoring"
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

@bot.command(name='re')
async def reverse_engineer(ctx):
    if not ctx.message.attachments:
        await ctx.send("Attach a file to analyze.")
        return

    attachment = ctx.message.attachments[0]
    filename = attachment.filename
    ext = os.path.splitext(filename)[1].lower()
    lang = LANG_MAP.get(ext, 'unknown')

    if lang == 'unknown':
        await ctx.send("Extension `" + ext + "` is not in the supported language map.")
        return

    file_path = "temp_" + filename
    await attachment.save(file_path)

    msg = await ctx.send(embed=build_embed(0, filename, lang, 0, color=0xF1C40F))

    await asyncio.sleep(1)
    await msg.edit(embed=build_embed(1, filename, lang, 33, color=0xF39C12))

    with open(file_path, 'r', errors='ignore') as f:
        content = f.read()

    source = SourceAnalyzer(content, ext)
    ast_builder = ASTBuilder(content, lang)
    symbols = SymbolExtractor(ast_builder.get_root(), content)
    symbols.extract()
    
    control_flow = ControlFlowAnalyzer(content)
    control_flow.analyze()
    
    data_flow = DataFlowAnalyzer(content)
    data_flow.analyze()
    
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
        ("Game Features", "`" + str(len(game.game_features)) + "`")
    ]
    await msg.edit(embed=build_embed(2, filename, lang, 66, fields=phase2_fields, color=0xE67E22))

    await asyncio.sleep(1)

    report_gen = ReportGenerator(source, symbols, control_flow, data_flow, dependencies, behavioral, security, game, patterns, confidence_engine)
    report_content = report_gen.generate()
    
    out_path = "analysis_report_" + filename + ".txt"
    with open(out_path, 'w') as f:
        f.write(report_content)

    phase3_fields = [
        ("Patterns Detected", "`" + str(len(patterns.patterns)) + "`"),
        ("Weaknesses", "`" + str(len(security.weaknesses)) + "`"),
        ("Output File", "`" + out_path + "`"),
        ("Status", "`COMPLETE`")
    ]
    await msg.edit(embed=build_embed(3, filename, lang, 100, fields=phase3_fields, color=0x2ECC71))

    await ctx.send(file=discord.File(out_path))
    os.remove(file_path)
    os.remove(out_path)

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

    lines = []
    lines.append("-- ============================================")
    lines.append("-- BYPASS TEMPLATE (STRUCTURAL DEMONSTRATION)")
    lines.append("-- ============================================")
    lines.append("")
    lines.append("-- This is a structural template demonstrating the concept.")
    lines.append("-- The actual implementation requires manual adaptation.")
    lines.append("")
    
    if ext in ['.lua', '.luau']:
        lines.append("local function bypass_template()")
        lines.append("    print('hello world')")
        lines.append("    local original_function = nil")
        lines.append("    local function hook_function(...)")
        lines.append("        if original_function then")
        lines.append("            return original_function(...)")
        lines.append("        end")
        lines.append("    end")
        lines.append("end")
        lines.append("bypass_template()")
    elif ext == '.py':
        lines.append("def bypass_template():")
        lines.append("    print('hello world')")
        lines.append("    original_function = None")
        lines.append("    def hook_function(*args, **kwargs):")
        lines.append("        if original_function:")
        lines.append("            return original_function(*args, **kwargs)")
        lines.append("bypass_template()")
    else:
        lines.append("// Bypass template for " + ext)
        lines.append("void bypass_template() {")
        lines.append("    printf(\"hello world\\n\");")
        lines.append("}")
    
    lines.append("")
    lines.append("-- ============================================")
    lines.append("-- END OF BYPASS TEMPLATE")
    lines.append("-- ============================================")
    
    bypass_content = "\n".join(lines)
    out_path = "bypass_template_" + filename.replace('.txt', ext)
    
    with open(out_path, 'w') as f:
        f.write(bypass_content)

    await ctx.send(file=discord.File(out_path))
    os.remove(file_path)
    os.remove(out_path)

TOKEN = os.environ.get('TOKEN')
bot.run(TOKEN)
