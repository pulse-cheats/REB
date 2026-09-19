import discord
import os
import re
import asyncio
import hashlib
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

SECURITY_PATTERNS = {
    'integrity_check': [
        r'checksum', r'hash\s*\(', r'integrity', r'verify',
        r'validate', r'signature', r'HMAC', r'SHA\d+', r'MD5',
        r'CRC\d+', r'crc32', r'crc64', r'bcrypt', r'argon2',
        r'file.?check', r'binary.?verify'
    ],
    'memory_guard': [
        r'ReadProcessMemory', r'WriteProcessMemory',
        r'VirtualProtect', r'VirtualAlloc', r'NtQueryInformation',
        r'CreateRemoteThread', r'OpenProcess', r'memcpy',
        r'memset', r'mmap', r'ptrace', r'proc_pidinfo',
        r'kvm_read', r'ReadMemory', r'WriteMemory', r'VirtualQuery',
        r'IsBadReadPtr', r'IsBadWritePtr'
    ],
    'speed_validation': [
        r'WalkSpeed', r'walkspeed', r'speed\s*[=<>]',
        r'velocity', r'movementSpeed', r'moveSpeed',
        r'CharacterMovement', r'MaxSpeed', r'GroundSpeed',
        r'AirSpeed', r'SwimSpeed', r'FlySpeed', r'acceleration',
        r'jump.?power', r'gravity', r'friction'
    ],
    'injection_detection': [
        r'DLL', r'dll', r'inject', r'LoadLibrary',
        r'GetProcAddress', r'dlopen', r'dlsym',
        r'module\s*load', r'hook', r'detour', r'trampoline',
        r'MinHook', r'Detours', r'EasyHook', r'PolyHook',
        r'LD_PRELOAD', r'dylib', r'shared.?object'
    ],
    'anti_debug': [
        r'IsDebuggerPresent', r'CheckRemoteDebuggerPresent',
        r'NtQueryInformationProcess', r'OutputDebugString',
        r'__debugbreak', r'int\s+3', r'PTRACE_TRACEME',
        r'SysBPM', r'NtSetInformationThread',
        r'HideFromDebugger', r'AntiDebug', r'TimingCheck',
        r'GetTickCount', r'QueryPerformanceCounter'
    ],
    'anti_tamper': [
        r'anti.?tamper', r'code\s*integrity',
        r'section\s*hash', r'page\s*guard',
        r'PAGE_GUARD', r'PAGE_NOACCESS',
        r'self.?check', r'binary.?check',
        r'obfuscat', r'packer', r'VMProtect',
        r'Themida', r'Enigma', r'ASPack', r'UPX',
        r'modify.?detect', r'patch.?detect'
    ],
    'network_validation': [
        r'server.?side', r'authoritative',
        r'reconcil', r'rollback', r'lag.?compensat',
        r'tick.?rate', r'sync', r'desync',
        r'heartbeat', r'keepalive', r'nonce',
        r'timestamp', r'sequence.?number', r'packet.?verify'
    ],
    'input_validation': [
        r'input.?sanitiz', r'rate.?limit',
        r'cooldown', r'throttle', r'debounce',
        r'max.?input', r'input.?clamp',
        r'clamp', r'normalize', r'saturate',
        r'bounds.?check', r'range.?check', r'limit.?check'
    ],
    'encryption': [
        r'AES', r'RSA', r'ECC', r'encrypt',
        r'decrypt', r'cipher', r'key\s*=',
        r'IV\s*=', r'salt', r'padding',
        r'block.?size', r'mode.?operation'
    ],
    'obfuscation': [
        r'xor', r'rotate', r'shift',
        r'encode', r'decode', r'base64',
        r'hex', r'mangle', r'scramble',
        r'string.?encrypt', r'control.?flow'
    ],
    'resource_protection': [
        r'asset.?lock', r'resource.?guard',
        r'file.?protect', r'model.?protect',
        r'script.?protect', r'code.?sign',
        r'digital.?signature', r'certificate'
    ],
    'behavior_analysis': [
        r'anomaly', r'pattern.?match',
        r'machine.?learn', r'statistical',
        r'deviation', r'baseline',
        r'threshold', r'outlier'
    ]
}

def build_embed(phase, filename, lang, progress_pct, fields=None, color=0x2F3136):
    embed = discord.Embed(
        title="Reverse Engineering Pipeline",
        description=f"Target: {filename}\nLanguage: {lang.upper()}",
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Phase {phase}/3 - {progress_pct}% complete")

    status = ["[ ]", "[ ]", "[ ]"]
    labels = [
        "Deep structural analysis",
        "Security mechanism mapping",
        "Comprehensive report generation"
    ]
    for i in range(phase):
        status[i] = "[x]"
    if phase < 3:
        status[phase] = "[...]"

    for i in range(3):
        embed.add_field(
            name=f"Task {i+1} {status[i]}",
            value=labels[i],
            inline=False
        )

    if fields:
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)

    return embed

def deep_analyze(content, ext):
    lang = LANG_MAP.get(ext, 'unknown')
    results = {
        'functions': [],
        'classes': [],
        'security': {},
        'entry_points': [],
        'imports': [],
        'constants': [],
        'globals': [],
        'control_flow': [],
        'data_flow': [],
        'dependencies': defaultdict(list),
        'total_lines': len(content.splitlines()),
        'total_chars': len(content),
        'complexity_score': 0,
        'security_score': 0,
        'cyclomatic_complexity': 0,
        'nesting_depth': 0,
        'function_calls': [],
        'string_literals': [],
        'numeric_constants': []
    }

    for category, patterns in SECURITY_PATTERNS.items():
        matches = []
        for pattern in patterns:
            found = re.findall(pattern, content, re.IGNORECASE)
            if found:
                matches.extend(found)
        if matches:
            unique = list(set(matches))
            results['security'][category] = unique[:20]
            results['security_score'] += len(unique) * 10

    results['string_literals'] = re.findall(r'["\']([^"\']{3,50})["\']', content)[:30]
    results['numeric_constants'] = re.findall(r'\b(\d+\.?\d*)\b', content)[:50]

    if TREE_SITTER and lang != 'unknown':
        try:
            parser = get_parser(lang)
            tree = parser.parse(bytes(content, "utf8"))
            root = tree.root_node

            def walk(node, depth=0, parent=None):
                if depth > results['nesting_depth']:
                    results['nesting_depth'] = depth
                
                if node.type in ['function_definition', 'method_definition',
                                 'function_declaration', 'function_item',
                                 'method_declaration', 'constructor_declaration']:
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        func_name = name_node.text.decode('utf8', errors='ignore')
                        func_info = {
                            'name': func_name,
                            'line': node.start_point[0] + 1,
                            'size': node.end_point[0] - node.start_point[0],
                            'params': [],
                            'calls': [],
                            'complexity': 0,
                            'returns': []
                        }
                        
                        for child in node.children:
                            if child.type in ['parameters', 'parameter_list']:
                                for param in child.children:
                                    if param.type in ['identifier', 'parameter']:
                                        func_info['params'].append(
                                            param.text.decode('utf8', errors='ignore')
                                        )
                            elif child.type in ['call', 'call_expression']:
                                call_name = child.child_by_field_name('function')
                                if call_name:
                                    call_text = call_name.text.decode('utf8', errors='ignore')
                                    func_info['calls'].append(call_text)
                                    results['function_calls'].append(call_text)
                            elif child.type in ['if_statement', 'for_statement', 
                                               'while_statement', 'switch_statement',
                                               'case_statement']:
                                func_info['complexity'] += 1
                                results['cyclomatic_complexity'] += 1
                            elif child.type in ['return_statement']:
                                func_info['returns'].append(
                                    child.text.decode('utf8', errors='ignore')[:50]
                                )
                        
                        results['functions'].append(func_info)
                        results['complexity_score'] += func_info['complexity']
                        
                elif node.type in ['class_definition', 'class_declaration',
                                   'struct_item', 'interface_declaration',
                                   'impl_item', 'enum_item']:
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        results['classes'].append({
                            'name': name_node.text.decode('utf8', errors='ignore'),
                            'line': node.start_point[0] + 1,
                            'methods': [],
                            'fields': []
                        })
                        
                elif node.type in ['import_statement', 'import_from_statement',
                                   'using_directive', 'use_declaration',
                                   'include_directive', 'require_statement']:
                    imp_text = node.text.decode('utf8', errors='ignore')[:100]
                    results['imports'].append(imp_text)
                    
                elif node.type in ['assignment', 'variable_declaration']:
                    var_name = node.child_by_field_name('name') or node.child_by_field_name('left')
                    if var_name:
                        results['globals'].append(
                            var_name.text.decode('utf8', errors='ignore')
                        )
                
                for child in node.children:
                    walk(child, depth + 1, node.type)

            walk(root)
        except Exception:
            pass

    if not results['functions']:
        func_patterns = [
            r'(?:def|function|func|fn|void|int|float|double|public|private|protected|static|async|local)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*function',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*function'
        ]
        for p in func_patterns:
            found = re.findall(p, content)
            for f in found:
                results['functions'].append({
                    'name': f, 
                    'line': 0, 
                    'size': 0,
                    'params': [],
                    'calls': [],
                    'complexity': 0,
                    'returns': []
                })

    if not results['imports']:
        imp = re.findall(r'(?:import|require|include|using|#include)\s+[<"\']([^>"\']+)[>"\']', content)
        results['imports'] = imp[:30]

    return results

def generate_comprehensive_report(results, ext, filename):
    lines = []
    lines.append("=" * 70)
    lines.append("COMPREHENSIVE STRUCTURAL ANALYSIS REPORT")
    lines.append("=" * 70)
    lines.append(f"Target File: {filename}")
    lines.append(f"Language: {LANG_MAP.get(ext, ext).upper()}")
    lines.append(f"Generated: {datetime.utcnow().isoformat()}")
    lines.append(f"Lines Analyzed: {results['total_lines']}")
    lines.append(f"Characters Analyzed: {results['total_chars']}")
    lines.append(f"Complexity Score: {results['complexity_score']}")
    lines.append(f"Cyclomatic Complexity: {results['cyclomatic_complexity']}")
    lines.append(f"Max Nesting Depth: {results['nesting_depth']}")
    lines.append(f"Security Score: {results['security_score']}")
    lines.append("=" * 70)
    lines.append("")

    sec = results['security']
    if sec:
        lines.append("SECURITY MECHANISMS IDENTIFIED")
        lines.append("-" * 70)
        lines.append(f"Total Vectors: {sum(len(v) for v in sec.values())}")
        lines.append("")

        category_labels = {
            'integrity_check': 'INTEGRITY / HASH CHECKS',
            'memory_guard': 'MEMORY PROTECTION',
            'speed_validation': 'SPEED / MOVEMENT VALIDATION',
            'injection_detection': 'INJECTION / HOOK DETECTION',
            'anti_debug': 'ANTI-DEBUG',
            'anti_tamper': 'ANTI-TAMPER / OBFUSCATION',
            'network_validation': 'NETWORK / SERVER AUTHORITY',
            'input_validation': 'INPUT VALIDATION / RATE LIMITS',
            'encryption': 'ENCRYPTION / CRYPTOGRAPHY',
            'obfuscation': 'OBFUSCATION TECHNIQUES',
            'resource_protection': 'RESOURCE PROTECTION',
            'behavior_analysis': 'BEHAVIOR ANALYSIS'
        }

        for cat, matches in sec.items():
            label = category_labels.get(cat, cat.upper())
            lines.append(f"[{label}]")
            lines.append(f"  Detection Surface: {len(matches)} vectors")
            for m in matches:
                lines.append(f"    - {m}")
            lines.append("")
    else:
        lines.append("No security mechanisms detected in surface scan.")
        lines.append("Code may use advanced obfuscation or runtime checks.")
        lines.append("")

    if results['functions']:
        lines.append("FUNCTION ANALYSIS")
        lines.append("-" * 70)
        lines.append(f"Total Functions: {len(results['functions'])}")
        lines.append("")
        
        security_relevant = []
        for fn in results['functions']:
            sec_relevance = []
            for cat, matches in sec.items():
                for m in matches:
                    if m.lower() in fn['name'].lower():
                        sec_relevance.append(cat)
            
            if sec_relevance:
                security_relevant.append((fn, sec_relevance))
        
        if security_relevant:
            lines.append("Security-Relevant Functions:")
            for fn, cats in security_relevant:
                line_info = f" (line {fn['line']})" if fn['line'] else ""
                lines.append(f"  - {fn['name']}{line_info}")
                lines.append(f"    Related to: {', '.join(set(cats))}")
                if fn['params']:
                    lines.append(f"    Parameters: {', '.join(fn['params'][:5])}")
                if fn['calls']:
                    lines.append(f"    Calls: {', '.join(fn['calls'][:5])}")
                lines.append(f"    Complexity: {fn['complexity']}")
                lines.append("")
        
        lines.append("All Functions:")
        for fn in results['functions'][:40]:
            line_info = f" (line {fn['line']}, {fn['size']} lines)" if fn['line'] else ""
            lines.append(f"  - {fn['name']}{line_info}")
        if len(results['functions']) > 40:
            lines.append(f"  ... and {len(results['functions']) - 40} more")
        lines.append("")

    if results['classes']:
        lines.append("CLASS / STRUCT ANALYSIS")
        lines.append("-" * 70)
        lines.append(f"Total Classes/Structs: {len(results['classes'])}")
        for cls in results['classes'][:25]:
            line_info = f" (line {cls['line']})" if isinstance(cls, dict) and cls.get('line') else ""
            name = cls['name'] if isinstance(cls, dict) else cls
            lines.append(f"  - {name}{line_info}")
        if len(results['classes']) > 25:
            lines.append(f"  ... and {len(results['classes']) - 25} more")
        lines.append("")

    if results['imports']:
        lines.append("DEPENDENCIES / IMPORTS")
        lines.append("-" * 70)
        lines.append(f"Total Imports: {len(results['imports'])}")
        for imp in results['imports'][:20]:
            lines.append(f"  - {imp}")
        if len(results['imports']) > 20:
            lines.append(f"  ... and {len(results['imports']) - 20} more")
        lines.append("")

    if results['globals']:
        lines.append("GLOBAL VARIABLES")
        lines.append("-" * 70)
        unique_globals = list(set(results['globals']))[:30]
        for g in unique_globals:
            lines.append(f"  - {g}")
        lines.append("")

    if results['string_literals']:
        lines.append("STRING LITERALS (SAMPLE)")
        lines.append("-" * 70)
        for s in results['string_literals'][:15]:
            lines.append(f"  - \"{s}\"")
        lines.append("")

    if results['numeric_constants']:
        lines.append("NUMERIC CONSTANTS (SAMPLE)")
        lines.append("-" * 70)
        unique_nums = list(set(results['numeric_constants']))[:20]
        for n in unique_nums:
            lines.append(f"  - {n}")
        lines.append("")

    if results['function_calls']:
        lines.append("FUNCTION CALL GRAPH")
        lines.append("-" * 70)
        call_counts = defaultdict(int)
        for call in results['function_calls']:
            call_counts[call] += 1
        sorted_calls = sorted(call_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        for call, count in sorted_calls:
            lines.append(f"  - {call}: {count} calls")
        lines.append("")

    lines.append("=" * 70)
    lines.append("END OF COMPREHENSIVE ANALYSIS REPORT")
    lines.append("=" * 70)
    
    return "\n".join(lines)

def generate_bypass_template(analysis_content, ext):
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
        lines.append("    ")
        lines.append("    -- Target functions identified in analysis:")
        lines.append("    -- Modify these hooks based on your specific requirements")
        lines.append("    ")
        lines.append("    local original_function = nil")
        lines.append("    ")
        lines.append("    local function hook_function(...)")
        lines.append("        -- Your logic here")
        lines.append("        if original_function then")
        lines.append("            return original_function(...)")
        lines.append("        end")
        lines.append("    end")
        lines.append("    ")
        lines.append("    -- Hook installation")
        lines.append("    -- original_function = target_function")
        lines.append("    -- target_function = hook_function")
        lines.append("end")
        lines.append("")
        lines.append("bypass_template()")
    elif ext == '.py':
        lines.append("def bypass_template():")
        lines.append("    print('hello world')")
        lines.append("    ")
        lines.append("    # Target functions identified in analysis:")
        lines.append("    # Modify these hooks based on your specific requirements")
        lines.append("    ")
        lines.append("    original_function = None")
        lines.append("    ")
        lines.append("    def hook_function(*args, **kwargs):")
        lines.append("        # Your logic here")
        lines.append("        if original_function:")
        lines.append("            return original_function(*args, **kwargs)")
        lines.append("    ")
        lines.append("    # Hook installation")
        lines.append("    # original_function = target_function")
        lines.append("    # target_function = hook_function")
        lines.append("")
        lines.append("bypass_template()")
    else:
        lines.append("// Bypass template for " + ext)
        lines.append("// This is a structural demonstration")
        lines.append("")
        lines.append("void bypass_template() {")
        lines.append("    printf(\"hello world\\n\");")
        lines.append("    ")
        lines.append("    // Target functions identified in analysis:")
        lines.append("    // Modify these hooks based on your specific requirements")
        lines.append("}")
    
    lines.append("")
    lines.append("-- ============================================")
    lines.append("-- END OF BYPASS TEMPLATE")
    lines.append("-- ============================================")
    
    return "\n".join(lines)

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
        await ctx.send(f"Extension `{ext}` is not in the supported language map.")
        return

    file_path = f"temp_{filename}"
    await attachment.save(file_path)

    msg = await ctx.send(embed=build_embed(0, filename, lang, 0, color=0xF1C40F))

    await asyncio.sleep(2)
    await msg.edit(embed=build_embed(1, filename, lang, 33, color=0xF39C12))

    with open(file_path, 'r', errors='ignore') as f:
        content = f.read()

    results = deep_analyze(content, ext)

    await asyncio.sleep(2)

    sec_count = sum(len(v) for v in results['security'].values())
    phase2_fields = [
        ("Functions Found", f"`{len(results['functions'])}`"),
        ("Classes / Structs", f"`{len(results['classes'])}`"),
        ("Security Vectors", f"`{sec_count}`"),
        ("Imports", f"`{len(results['imports'])}`"),
        ("Complexity Score", f"`{results['complexity_score']}`"),
        ("Cyclomatic Complexity", f"`{results['cyclomatic_complexity']}`"),
        ("Security Score", f"`{results['security_score']}`")
    ]
    await msg.edit(embed=build_embed(2, filename, lang, 66, fields=phase2_fields, color=0xE67E22))

    await asyncio.sleep(2)

    report_content = generate_comprehensive_report(results, ext, filename)
    out_path = f"comprehensive_analysis_{filename}.txt"
    with open(out_path, 'w') as f:
        f.write(report_content)

    sec_categories = list(results['security'].keys())
    phase3_fields = [
        ("Mechanisms Identified", f"`{len(sec_categories)}`"),
        ("Security-Relevant Functions", f"`{sum(1 for fn in results['functions'] if any(m.lower() in fn['name'].lower() for cat in results['security'].values() for m in cat))}`"),
        ("Output File", f"`{out_path}`"),
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

    file_path = f"temp_{filename}"
    await attachment.save(file_path)

    with open(file_path, 'r', errors='ignore') as f:
        content = f.read()

    lang_match = re.search(r'Language:\s*(\w+)', content)
    ext = '.lua'
    if lang_match:
        lang = lang_match.group(1).lower()
        for e, l in LANG_MAP.items():
            if l == lang:
                ext = e
                break

    bypass_content = generate_bypass_template(content, ext)
    out_path = f"bypass_template_{filename.replace('.txt', ext)}"
    
    with open(out_path, 'w') as f:
        f.write(bypass_content)

    await ctx.send(file=discord.File(out_path))
    os.remove(file_path)
    os.remove(out_path)

TOKEN = 'MTU1MDgxNjY2NTYxMTAxNDE4NQ.GNNCBq.KGvZ6LQb2n9E2VVIoO1A4MOD4FBpC54Jm7UuLI'
bot.run(TOKEN)
