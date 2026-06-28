from pathlib import Path
import re
import toml
import os
from loguru import logger

class BaseProfile:
    def __init__(self):
        self.decl_info = {}
        self.ref_file_map = {}
        self.ancestor_map = {}

    @staticmethod
    def key_ftype(key):
        return key[0] if isinstance(key, tuple) and len(key) >= 1 else None

    @staticmethod
    def key_name(key):
        return key[1] if isinstance(key, tuple) and len(key) >= 2 else None

    @staticmethod
    def key_matches(key, ftype=None, name=None):
        if not isinstance(key, tuple) or len(key) < 2:
            return False
        if ftype is not None and key[0] != ftype:
            return False
        if name is not None and key[1] != name:
            return False
        return True

    def find_entries_by_name(self, file_name, ftype=None, name=None):
        """Return [(key, Function), ...] entries matching a declaration/function name."""
        function_dictionary = self.decl_info.get(file_name, {})
        matches = []
        for key, function in function_dictionary.items():
            if self.key_matches(key, ftype=ftype, name=name):
                matches.append((key, function))
        return matches

    def find_first_entry_by_name(self, file_name, ftype=None, name=None):
        matches = self.find_entries_by_name(file_name, ftype=ftype, name=name)
        if not matches:
            return None, None
        return matches[0]

    def resolve_entry(self, file_name, key_or_name, default_ftype=None):
        """Resolve a full signature key, an old (ftype, name) key, or a bare name."""
        function_dictionary = self.decl_info.get(file_name, {})

        if isinstance(key_or_name, tuple):
            exact = function_dictionary.get(key_or_name)
            if exact is not None:
                return key_or_name, exact
            if len(key_or_name) >= 2:
                return self.find_first_entry_by_name(file_name, ftype=key_or_name[0], name=key_or_name[1])
            return None, None

        return self.find_first_entry_by_name(file_name, ftype=default_ftype, name=key_or_name)

    def resolve_entries(self, file_name, key_or_name, default_ftype=None):
        """Resolve to all candidate entries, useful for overloaded functions."""
        function_dictionary = self.decl_info.get(file_name, {})

        if isinstance(key_or_name, tuple):
            exact = function_dictionary.get(key_or_name)
            if exact is not None:
                return [(key_or_name, exact)]
            if len(key_or_name) >= 2:
                return self.find_entries_by_name(file_name, ftype=key_or_name[0], name=key_or_name[1])
            return []

        return self.find_entries_by_name(file_name, ftype=default_ftype, name=key_or_name)

    def get_ancestors(self):
        """
        Build a reverse-reference map using signature-aware function keys.

        Shape:
            ancestor_map[target_signature_key] = [
                (referencing_signature_key, referencing_file_name),
                ...
            ]

        Because calls/identifier uses are still name-based, a reference to an
        overloaded name is conservatively attached to all matching overloads.
        """
        self.ancestor_map = {}

        # First create one key per concrete function definition. This prevents
        # overloaded functions with the same name from sharing the same map key.
        for file_name, function_dictionary in self.decl_info.items():
            for key, function_node in function_dictionary.items():
                ftype = self.key_ftype(key)
                func_name = self.key_name(key)
                if ftype != "func" or func_name == "LLVMFuzzerTestOneInput":
                    continue
                self.ancestor_map.setdefault(key, [])

        # Then add reverse references. Since expr_used stores only identifier
        # names, not resolved overload signatures, match by name and add the
        # current function as an ancestor of every candidate target overload.
        target_keys = list(self.ancestor_map.keys())
        for file_name, function_dictionary in self.decl_info.items():
            for source_key, function_node in function_dictionary.items():
                source_ftype = self.key_ftype(source_key)
                source_name = self.key_name(source_key)
                if source_ftype != "func" or source_name == "LLVMFuzzerTestOneInput":
                    continue

                for expr in function_node.expr_used or []:
                    referenced_name = expr.split(";")[0]
                    if referenced_name == source_name:
                        continue

                    for target_key in target_keys:
                        if self.key_name(target_key) != referenced_name:
                            continue
                        u = set(self.ancestor_map[target_key])
                        u.add((source_key, file_name))
                        self.ancestor_map[target_key] = list(u)

    def get_enclosing_function(self, file_name: str, line: int = None, name: str = None):
        last_option = None
        for key, function in self.decl_info[file_name].items():
            ftype = self.key_ftype(key)
            func_name = self.key_name(key)
            if name is not None:
                if ftype == "func" and name == func_name:
                    return function
                if name == func_name:
                    last_option = function
            else:
                if ftype == "func" and function.encloses(line):
                    return function
        if last_option is None:
            return None
        else:
            logger.warning("Returning entry that is not marked as a function")
            return last_option

    def get_used_types(self, file_name: str, func_name, used_types, lineno=None):
        _, function_info = self.resolve_entry(file_name, func_name)
        if function_info is not None:
            for inf_key in ("types_used", "expr_used"):
                values = getattr(function_info, inf_key) or []
                if lineno is not None:
                    for types in values:
                        if int(types.split(";")[1]) <= lineno:
                            name = types.split(";")[0]
                            if name not in used_types:
                                used_types.add(name)
                                used_types.update(self.get_used_types(file_name, name, used_types, lineno))
                else:
                    for types in values:
                        name = types.split(";")[0]
                        if name not in used_types:
                            used_types.add(name)
                            used_types.update(self.get_used_types(file_name, name, used_types, lineno))

        return used_types

    def internal_function_references(self, file_name, used_types, line):
        _ = line
        ref_functions = set()
        for name in used_types:
            for ftype in ["decl", "func", "extern", "func_decl"]:
                for key, function_info in self.find_entries_by_name(file_name, ftype=ftype, name=name):
                    ref_functions.add(key)
                    args = function_info.function_ptrs or []
                    for arg in args:
                        arg_name = arg.split(";")[0]
                        for arg_key, _ in self.find_entries_by_name(file_name, ftype="func", name=arg_name):
                            ref_functions.add(arg_key)
        return ref_functions

    def get_calls_recursively(self, file_name: str, node, lineno=None, all_calls=None):
        if all_calls is None:
            all_calls = set()

        candidates = self.resolve_entries(file_name, node, default_ftype="func")
        for node_key, function_info in candidates:
            if node_key in all_calls:
                continue
            all_calls.add(node_key)

            if function_info is None:
                continue

            if lineno is not None:
                filtered_call_names = {
                    call.split(";")[0]
                    for call in (function_info.calls or [])
                    if int(call.split(";")[1]) <= lineno
                }
                call_argument_names = {
                    call.split(";")[0]
                    for call in (function_info.function_ptrs or [])
                    if int(call.split(";")[1]) <= lineno
                }
            else:
                filtered_call_names = {call.split(";")[0] for call in (function_info.calls or [])}
                call_argument_names = {call.split(";")[0] for call in (function_info.function_ptrs or [])}

            for call_name in filtered_call_names:
                # Without argument-type inference, a call by name may resolve to
                # multiple overload candidates. Include all local candidates.
                for sub_func_key, _ in self.find_entries_by_name(file_name, ftype="func", name=call_name):
                    if sub_func_key not in all_calls:
                        all_calls = all_calls.union(
                            self.get_calls_recursively(file_name, sub_func_key, all_calls=all_calls)
                        )

                for sub_func_decl_key, _ in self.find_entries_by_name(file_name, ftype="func_decl", name=call_name):
                    all_calls.add(sub_func_decl_key)

            # Needed for function pointers.
            for arg_name in call_argument_names:
                for sub_func_key, _ in self.find_entries_by_name(file_name, ftype="func", name=arg_name):
                    if sub_func_key not in all_calls:
                        all_calls = all_calls.union(
                            self.get_calls_recursively(file_name, sub_func_key, all_calls=all_calls)
                        )
                for sub_func_decl_key, _ in self.find_entries_by_name(file_name, ftype="func_decl", name=arg_name):
                    all_calls.add(sub_func_decl_key)

        return {tf for tf in all_calls}

class PreprocessUtils:
    @staticmethod
    def fix_srcml_bugs(lines):
        whole = "".join(lines)
        output = []

        # Ensure not splitting by quotation within '' through lookahead
        parts = PreprocessUtils._split_code_lines(whole)
        for line in parts:
            # Do not handle single quotes ' can still have the same issue
            result = PreprocessUtils._fix_quotes(line)
            # result = PreprocessUtils._fix_gnu(result, "__extension__")
            result = PreprocessUtils._fix_gnu(result, "__attribute__")
            result = result.replace(".__sigaction_handler", "")
            output.append(result)
        whole = "".join(output)
        return whole
    
    @staticmethod
    def find_statements_by_start_pattern(text, start_pattern):
        matches = []
        start_positions = [i for i in range(len(text)) if text.startswith(start_pattern, i)]

        for start in start_positions:
            i = text.find('(', start) + 1
            stack = ['(']

            while stack and i < len(text):
                if text[i] == '(':
                    stack.append('(')
                elif text[i] == ')':
                    stack.pop()
                i += 1

            matches.append(text[start:i])

        return matches

    @staticmethod
    def is_valid_nested(text):
        stack = []
        opening = {'(': ')', '{': '}'}
        closing = {')', '}'}

        for char in text:
            if char in opening:
                stack.append(char)
            elif char in closing:
                if not stack:
                    return False
                last_open = stack.pop()
                if opening[last_open] != char:
                    return False

        return not stack
    
    @staticmethod
    def find_statements_by_pattern(text, pattern):
        candidate_matches = re.findall(pattern, text, flags=re.MULTILINE | re.DOTALL)
        valid_matches = [match for match in candidate_matches if PreprocessUtils.is_valid_nested(match)]
        return valid_matches
    
    @staticmethod
    def insert_line_before_match(whole, match, new_line):
        _idx = whole.find(match)
        if _idx == -1:
            logger.warning(f"Match not found in the text")
            return whole

        lines = whole.splitlines(True)        
        line_number = 0
        current_length = 0
        for i, line in enumerate(lines):
            current_length += len(line)
            if current_length > _idx:
                line_number = i
                break

        for j in range(line_number - 1, -1, -1):
            # get previous statement or block
            # ; can end up matching stuff like for(;;) which is not a statement
            # { prevents exiting the current block
            # } prevents entering the previous block
            if lines[j].strip().endswith((";", "{", "}")):
                lines.insert(j + 1, new_line.strip() + "\n")
                break

        whole = "".join(lines)
        return whole

    @staticmethod
    def replace_statements(whole, pattern, defines, start_idx=0, inplace=True, strategy='regex'):
        if strategy == 'regex':
            valid_matches = PreprocessUtils.find_statements_by_pattern(whole, pattern)
        elif strategy == 'start_pattern':
            valid_matches = PreprocessUtils.find_statements_by_start_pattern(whole, pattern)
        else:
            raise ValueError("Invalid strategy")
        if not valid_matches:
            return whole, start_idx
        pending_definitions = []
        for idx, match in enumerate(valid_matches, start=start_idx):
            _fixed_match = match.replace("\n", "\\\n")
            replacement = f"sniptest_{idx}_replacement"
            _line = f"#define {replacement} {_fixed_match}"
            if not inplace:
                whole = whole.replace(match, replacement, 1)
                defines.append(_line)
            else:
                _lines_missed_in_match = len(match.splitlines()) - 1
                whole = whole.replace(match, replacement + "\n"*_lines_missed_in_match, 1)
                pending_definitions.append((replacement, _line))
        for replacement, _line in pending_definitions:
            whole = PreprocessUtils.insert_line_before_match(whole, replacement, _line)
        return whole, idx + 1

    
    @staticmethod
    def fix_ts_bugs(lines):
        output = []
        for line in lines:
            # TODO: port this over to the new valid_matches logic 
            # but we don't consider if we are working on a string in the current implementation
            result = PreprocessUtils._fix_gnu(line, "__attribute__")
            # result = result.replace("__extension__", "sniptest_extension_replacement")
            output.append(result)
        whole = "".join(output)
        defines = [
            # "#ifndef sniptest_extension_replacement",
            # "#define sniptest_extension_replacement __extension__",
            # "#endif",
        ]
        current_idx = 0

        # extension_statements_pattern = r'(sniptest_extension_replacement\s*\(\s*\{.*?\}\))'
        # expression_statements_pattern = r'(\(\{.*?\}\))'
        asm_start_pattern = '__asm__'

        # for pattern in [extension_statements_pattern, expression_statements_pattern]:
        #     whole, current_idx = PreprocessUtils.replace_statements(
        #         whole, pattern, defines, 
        #         start_idx=current_idx
        #     )

        for start_pattern in [asm_start_pattern]:
            whole, current_idx = PreprocessUtils.replace_statements(
                whole, start_pattern, defines, 
                start_idx=current_idx, 
                inplace=False, 
                strategy='start_pattern'
            )

        _definitions = "\n".join(defines)
        return _definitions + "\n" + whole

    @staticmethod
    # __extension__
    # __attribute__ macros are misparsed by srcml. this function fixes that
    def _fix_gnu(s, macro):
        _len = len(macro)
        if macro in s:
            ret = ""
            skip = 0
            find_pattern = 0
            seq_found = 0
            for i in range(len(s)):
                c = s[i]
                if i + _len < len(s) and s[i : i + _len] == macro:
                    find_pattern = 1
                    seq_found = 0
                if find_pattern == 1:
                    if c == "(":
                        skip += 1
                        seq_found = 1
                        continue
                    elif c == ")" and skip > 0:
                        skip -= 1
                        continue
                    elif skip == 0 and seq_found == 1:
                        find_pattern = 0
                    else:
                        continue
                ret = ret + c
            return ret
        return s

    @staticmethod
    # Some lines with """ in them are misparsed by srcml. This function rectifies that.
    def _fix_quotes(line):
        matches = re.search(r'"[^"]*"[\s]*"', line)
        result = line
        if matches:
            # Final string that is result of operation
            result = ""
            # Number of " marks that we've encountered after an opening " mark was seen
            count = 0
            # If it is set it means that we have seen an opening " mark and need to close it
            close = 0
            # Number of newlines between adjoining " marks
            newlines = 0
            # if there is '"' then " must be ignored. when set this enables that
            ignore = 0
            i = 0
            while i < len(line):
                c = line[i]
                if c == "'":
                    if ignore == 0:
                        # Concatenate " marks together
                        if count > 0:
                            if count % 2 == 0:
                                result = result + ""
                            else:
                                result = result + '"'
                                close = 0
                        count = 0
                        ignore = 1
                    else:
                        ignore = 0

                if ignore == 1:
                    result = result + c
                    i = i + 1
                    continue

                if c == '"':
                    if close == 1:
                        count = count + 1
                    elif close == 0:
                        close = 1
                        result = result + '"'
                elif c == " ":
                    if count == 0:
                        result = result + c
                elif c == "\n":
                    if count == 0:
                        result = result + c
                    else:
                        newlines = newlines + 1
                elif c == "\t":
                    if count == 0:
                        result = result + c
                # In presence of backslash ignore effect of next character
                elif c == "\\":
                    result = result + c
                    c = line[i + 1]
                    result = result + c
                    i = i + 1
                else:
                    # Concatenate " marks together
                    if count > 0:
                        if count % 2 == 0:
                            result = result + ""
                        else:
                            result = result + '"'
                            close = 0
                    count = 0
                    result = result + c
                i = i + 1

            # End
            if count > 0:
                if count % 2 == 0:
                    result = result + ""
                else:
                    result = result + '"'
                    close = 0
            for j in range(newlines):
                result = result + "\n"

        return result

    @staticmethod
    # Split file into parts ending with ; and comments to correct later
    def _split_code_lines(whole):
        char_to_close = ""
        breaks = []
        ignore = 0

        i = 0
        while i < len(whole):
            c = whole[i]
            if ignore == 1 and c == char_to_close:
                ignore = 0
            elif ignore == 0 and c == "'":
                ignore = 1
                char_to_close = "'"
            elif ignore == 0 and c == '"':
                ignore = 1
                char_to_close = '"'
            elif c == "\\":
                i = i + 2
                continue

            if ignore == 1:
                i = i + 1
                continue

            if c == ";":
                breaks.append(i)
            elif c == "#":
                while whole[i] != "\n":
                    i = i + 1
                breaks.append(i - 1)
            elif c == "/":
                i = i + 1
                if whole[i] == "/":
                    while whole[i] != "\n":
                        i = i + 1
                    breaks.append(i - 1)
                if whole[i] == "*":
                    while (whole[i] != "/") or (whole[i - 1] != "*"):
                        i = i + 1
                    while whole[i] != "\n":
                        i = i + 1
                    breaks.append(i - 1)

            i = i + 1

        parts = []
        start = 0
        end = -1
        for i in breaks:
            end = i + 1
            parts.append(whole[start:end])
            start = end
        parts.append(whole[end : len(whole) - 1])
        return parts
    
class Toml:
    def save_to_toml_file(file_path, data):
        with open(file_path, "w") as file:
            toml.dump(data, file)


    def read_from_toml_file(file_path):
        data = {}
        assert os.path.isfile(file_path)
        with open(file_path, 'r') as f:
            data = toml.load(f)
        return data
    
class SnipTestPath:
    @staticmethod
    def replace_extension(path, new_extension):
        return str(Path(path).with_suffix(new_extension))
    
    @staticmethod
    def get_file_root(path):
        return Path(path).stem
