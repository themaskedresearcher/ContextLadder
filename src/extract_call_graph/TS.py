import traceback
import re

import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser, Tree

from tqdm import tqdm
from .AST import Function
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

C_LANGUAGE = Language(tsc.language())
CPP_LANGUAGE = Language(tscpp.language())
parser_c = Parser(C_LANGUAGE)
parser_cpp = Parser(CPP_LANGUAGE)

from loguru import logger


import os
from .utils import BaseProfile
import functools

def affects_tree(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._refresh()
        try:
            result = method(self, *args, **kwargs)
        finally:
            self._update()
        return result
    return wrapper

class TS(BaseProfile):
    def __init__(self, target: str):
        super().__init__()
        self.parse_directory(target)
        self.get_ancestors()

    @staticmethod
    def get_node_at_line(tree, line_number):
        """
        NOT TESTED: DO NOT USE

        Retrieves the node at the specified line number.

        Args:
            tree: The syntax tree parsed by Tree-sitter.
            line_number: The line number for which the node is to be found.

        Returns:
            The node at the specified line number, or None if no node is found.
        """
        cursor = tree.walk()
        while cursor.goto_first_child():
            node = cursor.node
            if node.start_point[0] <= line_number <= node.end_point[0]:
                # If the node is within the desired line range, keep going deeper.
                while cursor.goto_first_child():
                    child_node = cursor.node
                    if child_node.start_point[0] <= line_number <= child_node.end_point[0]:
                        node = child_node
                    else:
                        cursor.goto_next_sibling()
                        node = cursor.node
                        break
                return node
            elif cursor.goto_next_sibling():
                continue
            else:
                break
        return None

    @staticmethod
    def track_comment(tree, comment_start, language="c"):
        """
        Tracks the comment node starting with '/*target_line*/'.

        Args:
            tree: The syntax tree parsed by Tree-sitter.
            comment_start: The string that identifies the target comment.

        Returns:
            The node containing the target comment, or None if not found.
        """
        captures = TS.query_tree(
            tree, 
            f"""
            (comment) @comment
            """,
            language
        )
        if not captures or "comment" not in captures:
            return None
        for node in captures["comment"]:
            comment_text = node.text.decode("utf8")
            if comment_text.startswith(comment_start):
                return node
        return None

    @staticmethod
    def get_parent_statement(node):
        """
        If nodes parent is anything other than a statement ending with a semicolon or a compound statement,
        this function will return None. Otherwise, it will return the parent node obtained by traversing up the tree.
        """
        while not node.text.decode("utf8").endswith(";"):
            if node.parent is None:
                return None
            elif node.parent.type == "translation_unit":
                return None
            elif node.parent.type == "compound_statement":
                return node.parent
            elif node.parent.type == "labeled_statement":
                return node.parent
            node = node.parent
        return node


    @staticmethod
    def get_tree(code, language = "c"):
        language = language.lower()
        if language == "c":
            tree = parser_c.parse(bytes(code, 'utf-8'))
        elif language == "cpp":
            tree = parser_cpp.parse(bytes(code, 'utf-8'))
        else:
            raise ValueError("Invalid language")
        return tree
    
    @staticmethod
    def query_tree(tree: Tree, query, language):
        language = language.lower()
        if language == "c":
            query = C_LANGUAGE.query(query)
        elif language == "cpp":
            query = CPP_LANGUAGE.query(query)
        else:
            raise ValueError("Invalid language")
        return query.captures(tree.root_node)

    @staticmethod
    def parse_file(file_path):
        if file_path.endswith(('.c', '.h')):
            language = "c"
        elif file_path.endswith('.cpp'):
            language = "cpp"
        else:
            raise ValueError("Invalid file extension")
        
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
        code = TS._fix_code(code, language, file_path)

        return TS.get_tree(code, language)
    
    @staticmethod
    def traverse_tree(tree: Tree):
        cursor = tree.walk()

        reached_root = False
        while reached_root == False:
            yield cursor.node

            if cursor.goto_first_child():
                continue

            if cursor.goto_next_sibling():
                continue

            retracing = True
            while retracing:
                if not cursor.goto_parent():
                    retracing = False
                    reached_root = True

                if cursor.goto_next_sibling():
                    retracing = False
    
    @staticmethod
    def _remove_marked_ranges(marked_ranges_for_deletion, code, filter_empty_lines=True):
        code_lines = code.splitlines(True)
        for start, end in marked_ranges_for_deletion:
            if start.row == end.row:
                # Replace characters within the same line with whitespace
                line = code_lines[start.row]
                code_lines[start.row] = (
                    line[:start.column] +
                    ' ' * (end.column - start.column) +
                    line[end.column:]
                )
            else:
                # Replace from start column to the end of the start row with whitespace
                line = code_lines[start.row]
                code_lines[start.row] = line[:start.column] + ' ' * (len(line) - start.column)

                # Replace entire rows between start and end with whitespace
                for row in range(start.row + 1, end.row):
                    code_lines[row] = ' ' * len(code_lines[row])

                # Replace from the beginning of the end row to the end column with whitespace
                line = code_lines[end.row]
                code_lines[end.row] = ' ' * end.column + line[end.column:]

        # Reassemble the code string, filtering out empty lines
        if filter_empty_lines:
            code = "".join(line for line in code_lines if line.strip())
        else:
            code = "".join(code_lines)
        return code

    @staticmethod
    def _fix_code(code, language, file_path):
        og_tree = TS.get_tree(code, language)

        for node in TS.traverse_tree(og_tree):
            if node.type == "ERROR":
                _node_text = TS.get_node_text(node)
                _trimmed_log = _node_text[:100]
                if len(_node_text) > 100:
                    _trimmed_log += "..."

                if node.parent is not None:
                    _parent_text = TS.get_node_text(node.parent)
                else:
                    _parent_text = "<NO_PARENT>"

                _trimmed_parent_log = _parent_text[:100]
                if len(_parent_text) > 100:
                    _trimmed_parent_log += "..."

                logger.warning(
                    "Error in parsing {}:{}:{} | {} | {}",
                    file_path,
                    node.start_point[0],
                    node.start_point[1],
                    _trimmed_log,
                    _trimmed_parent_log
                )

        return code

    def default_dict_list_union(original_dict, new_dict):
        for key, value in new_dict.items():
            if key in original_dict:
                original_dict[key].extend(value)  # Join the existing value with the new one
            else:
                original_dict[key] = value  # Add the new key-value pair if the key doesn't exist
    
    def parse_directory(self, target):
        logger.info("Running TS on {}", target)

        num_cpus = os.cpu_count() or 4
        max_workers = max(2, min(num_cpus, 16))
        # max_workers = 1

        # if os.path.exists(FuzzConfig.locations["ignored_dirs"]):
        #     with open(FuzzConfig.locations["ignored_dirs"], 'r') as f:
        #         ignored_dirs = f.read().splitlines()

        file_paths=[]
        for root, _, files in os.walk(target):
            # if any(ignored_dir in root for ignored_dir in ignored_dirs):
            #     continue
            for file in files:
                if file.endswith(('.c', '.cpp', '.h')):
                    file_paths.append(os.path.join(root, file))
        total_files = len(file_paths)

        with tqdm(total=total_files, desc="Processing files") as pbar:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {executor.submit(TS.process_file, file_path): file_path for file_path in file_paths}                
                for future in as_completed(future_to_file):
                    pbar.update(1)
                    try:
                        try:
                            decl_info, ref_file_map = future.result()
                        except Exception as exc:
                            print("line 1: ", exc)
                            file_path = future_to_file[future]
                            print(f"\n[ERROR] Failed while processing file: {file_path}")
                            traceback.print_exception(type(exc), exc, exc.__traceback__)
                            continue
                            continue
                        try:
                            self.decl_info.update(decl_info)
                        except Exception as exc:
                            print("line 2: ", exc)
                            continue
                        try:
                            TS.default_dict_list_union(self.ref_file_map, ref_file_map)
                        except Exception as exc:
                            print("line 3: ", exc)
                            continue
                    except Exception as exc:
                        logger.error('File {} generated an exception: {}', future_to_file[future], exc)
    
    @staticmethod
    def _find_brace_block_end(lines, open_line_idx):
        depth = 0
        for idx in range(open_line_idx, len(lines)):
            depth += lines[idx].count("{")
            depth -= lines[idx].count("}")
            if depth == 0 and idx > open_line_idx:
                return idx
        return None

    @staticmethod
    def supplement_kr_functions(code, file_path, decl_info, ref_file_map):
        """Index old-style (K&R) C functions that tree-sitter does not model as function_definition."""
        if not file_path.endswith(".c"):
            return

        skip_names = {"if", "while", "for", "switch", "return", "sizeof"}
        lines = code.splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            match = re.match(r"^([A-Za-z_]\w*)\s*\(([^)]*)\)\s*$", stripped)
            if not match or match.group(1) in skip_names:
                i += 1
                continue

            func_name = match.group(1)
            brace_line = i + 1
            while brace_line < len(lines) and lines[brace_line].strip() and not lines[brace_line].strip().startswith("{"):
                brace_line += 1
            if brace_line >= len(lines) or not lines[brace_line].strip().startswith("{"):
                i += 1
                continue

            ret_line = i - 1
            while ret_line >= 0:
                candidate = lines[ret_line].strip()
                if not candidate or candidate.startswith("/*") or candidate.startswith("*") or candidate.startswith("//"):
                    ret_line -= 1
                    continue
                break
            if ret_line < 0:
                i += 1
                continue

            prev = lines[ret_line].strip()
            if prev.endswith((";", "{", "}")) or prev.startswith("#"):
                i += 1
                continue

            end_line = TS._find_brace_block_end(lines, brace_line)
            if end_line is None:
                i += 1
                continue

            existing = decl_info.get(file_path, {})
            if any(isinstance(key, tuple) and key[0] == "func" and key[1] == func_name for key in existing):
                i = end_line + 1
                continue

            body = "\n".join(lines[brace_line : end_line + 1])
            try:
                body_tree = parser_c.parse(bytes(body, "utf-8"))
            except Exception:
                i = end_line + 1
                continue

            calls = []
            for call in TS.get_instances(body_tree.root_node, "call_expression"):
                fn_node = call.child_by_field_name("function")
                if fn_node is not None:
                    calls.append(fn_node.text.decode("utf-8", errors="replace") + ";" + str(call.start_point[0] + brace_line))

            return_type = prev
            signature_key = TS.make_signature_key("func", func_name, return_type, False, tuple())
            function = Function(
                func_name,
                file_path,
                "func",
                [ret_line, 0],
                [end_line, max(0, len(lines[end_line]) - 1)],
                True,
                False,
                return_type=return_type,
                param_types=tuple(),
                signature_key=signature_key,
            )
            function.calls = list(set(calls))
            function.types_used = TS.collect_non_primitive_type_identifiers(body_tree.root_node)
            expr_used = TS.get_instances(body_tree.root_node, "identifier")
            function.expr_used = list(
                set(e.text.decode("utf-8", errors="replace") + ";" + str(e.start_point[0] + brace_line) for e in expr_used)
            )
            function.parameter = []

            if file_path not in decl_info:
                decl_info[file_path] = {}
            decl_info[file_path][signature_key] = function
            if signature_key not in ref_file_map:
                ref_file_map[signature_key] = []
            ref_file_map[signature_key].append(file_path)
            i = end_line + 1

    @staticmethod
    def process_file(file_path):
        # if "lib/legacy" in file_path:
        #     print(file_path)
        decl_info = {}
        ref_file_map = {}
        tree = TS.parse_file(file_path)
        for node in tree.root_node.children:
            TS.process_node(node, file_path, TS.get_all_defined_functions_and_range, {
                "decl_info": decl_info,
                "ref_file_map": ref_file_map
            })
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            source_code = f.read()
        try:
            TS.supplement_kr_functions(source_code, file_path, decl_info, ref_file_map)
        except Exception:
            logger.warning("K&R supplement failed for {}", file_path)
        # if "lib/legacy" in file_path:
        #     print(file_path, ref_file_map, decl_info)
        return decl_info, ref_file_map
    
    @staticmethod
    def process_node(node, file_path, processing_function, additional_args=None):
        if additional_args is None:
            additional_args = {}
        if node.type in ["function_definition", "function_declaration", "declaration", "struct_specifier", "enum_specifier", "union_specifier", "type_definition", "macro_type_specifier"]:
            func_name = TS.get_name(node)
            ftype, is_function, is_static = TS.determine_type_and_attributes(node)

            args = {
                "node": node,
                "file_path": file_path,
                "func_name": func_name,
                "ftype": ftype,
                "is_function": is_function,
                "is_static": is_static
            }
            all_args = {**args, **additional_args}
            processing_function(**all_args)            
        else:
            for child in node.children:
                TS.process_node(child, file_path, processing_function, additional_args)

    @staticmethod
    def get_all_defined_functions_and_range(node, file_path, func_name, ftype, is_function, is_static, decl_info, ref_file_map, **kwargs):
        if func_name and ftype:

            if node.type == "declaration":
                # check for multiple variables declared in the same line
                declarators = node.children_by_field_name('declarator')
                func_names = [
                    TS.get_name(declarator) for declarator in declarators
                ]
            else:
                func_names = [func_name]

            declarator_by_name = {}
            if node.type == "declaration":
                for declarator in node.children_by_field_name('declarator'):
                    declarator_by_name[TS.get_name(declarator)] = declarator

            for func_name in func_names:
                declarator = declarator_by_name.get(func_name)

                if func_name == "main" and ftype == "func":
                    logger.info("Renaming main to original_main at {}", file_path)
                    func_name = "original_main"

                signature_key = TS.get_signature_key(
                    node=node,
                    ftype=ftype,
                    func_name=func_name,
                    is_static=is_static,
                    declarator=declarator,
                )
                return_type = signature_key[2]
                param_types = signature_key[4]

                if signature_key not in ref_file_map and is_static == 0:
                    ref_file_map[signature_key] = []
                if is_static == 0:
                    ref_file_map[signature_key].append(file_path)

                _function = Function(
                        func_name,
                        file_path,
                        ftype,
                        TS.get_pos(node.start_point),
                        TS.get_pos(node.end_point),
                        is_function,
                        is_static,
                        return_type=return_type,
                        param_types=param_types,
                        signature_key=signature_key
                    )

                # TODO: check if these work
                # _function.labels = TS.get_instances(node, "label")
                _calls = TS.get_instances(node, "call_expression")
                _function.calls = list(set([c.child_by_field_name('function').text.decode('utf-8')+";"+str(c.start_point[0]) for c in _calls if c.child_by_field_name('function')]))
                
                _function.types_used = TS.collect_non_primitive_type_identifiers(node)

                _expr_used = TS.get_instances(node, "identifier")
                _function.expr_used = list(set([e.text.decode('utf-8')+";"+str(e.start_point[0]) for e in _expr_used]))

                _function.parameter = TS.get_parameters(node, ftype in ["func", "func_decl"])

                if not decl_info.get(file_path):
                    decl_info[file_path] = {}
                        
                if decl_info[file_path].get(signature_key, None) is not None:
                    logger.warning("Existing declaration replaced {}", signature_key)
                    
                decl_info[file_path][signature_key] = _function

    @staticmethod
    def collect_non_primitive_type_identifiers(node):
        _types_used = TS.get_instances(node, "type_identifier")
        return list(set([t.text.decode('utf-8')+";"+str(t.start_point[0]) for t in _types_used]))

    @staticmethod
    def find_declarator(node):
        if node.child_by_field_name('declarator'):
            return node.child_by_field_name('declarator')
        # If 'declarator' is not a field, search recursively in children
        for child in node.children:
            result = TS.find_declarator(child)
            if result:
                return result
        return None

    @staticmethod
    def get_name(node):
        if 'identifier' in node.type:
            return node.text.decode('utf-8')
        
        if node.child_by_field_name('name'):
            return node.child_by_field_name('name').text.decode('utf-8')
        
        # Check for declarators
        declarator = TS.find_declarator(node)
        if declarator:
            return TS.get_name(declarator)
        
        # Fallback logic (should not be reached)
        for child in node.children:
            name = TS.get_name(child)
            if name is not None:
                return name
        return None

    @staticmethod
    def get_node_text(node):
        if node is None:
            return ""
        return node.text.decode("utf-8", errors="replace")

    @staticmethod
    def normalize_type(type_text: str) -> str:
        """Return a stable, whitespace-normalized type string for signature keys."""
        if type_text is None:
            return ""
        return " ".join(str(type_text).replace("\n", " ").split())

    @staticmethod
    def _declarator_pointer_prefix(declarator) -> str:
        """Collect leading pointer/reference markers attached to a declarator."""
        if declarator is None:
            return ""
        text = TS.get_node_text(declarator).strip()
        prefix = ""
        i = 0
        while i < len(text) and text[i] in "*&":
            prefix += text[i]
            i += 1
        return prefix

    @staticmethod
    def get_return_type(node) -> str:
        """Extract the declared return/base type from a function or declaration node."""
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return TS.normalize_type(TS.get_node_text(type_node))

        declarator = TS.find_declarator(node)
        parts = []
        for child in node.children:
            if child == declarator:
                break
            if child.type not in ["storage_class_specifier"]:
                parts.append(TS.get_node_text(child))
        return TS.normalize_type(" ".join(parts))

    @staticmethod
    def get_param_types_from_declarator(declarator) -> tuple:
        """Extract only parameter types from a function declarator."""
        if declarator is None:
            return tuple()

        while declarator is not None and declarator.type == "pointer_declarator":
            declarator = declarator.child_by_field_name("declarator")

        if declarator is None:
            return tuple()

        params_node = declarator.child_by_field_name("parameters")
        if params_node is None:
            return tuple()

        param_types = []
        for param in params_node.named_children:
            if param.type != "parameter_declaration":
                # Variadic marker, e.g. ...
                if TS.get_node_text(param).strip() == "...":
                    param_types.append("...")
                continue

            if len(param.children) == 1:
                param_type = TS.get_node_text(param.children[0])
            else:
                *type_nodes, name_or_declarator = param.children
                param_type = "".join(TS.get_node_text(t) for t in type_nodes)
                param_type += TS._declarator_pointer_prefix(name_or_declarator)

            param_types.append(TS.normalize_type(param_type))

        # In C/C++, f(void) has no callable argument types.
        if len(param_types) == 1 and param_types[0] == "void":
            return tuple()
        return tuple(param_types)

    @staticmethod
    def get_param_types(node, is_function_like: bool, declarator=None) -> tuple:
        if not is_function_like:
            return tuple()
        if declarator is None:
            declarator = TS.find_declarator(node)
        return TS.get_param_types_from_declarator(declarator)

    @staticmethod
    def make_signature_key(ftype, func_name, return_type="", is_static=False, param_types=()):
        """Create the canonical declaration/function key used in decl_info/ref_file_map."""
        return (
            ftype,
            func_name,
            TS.normalize_type(return_type),
            bool(is_static),
            tuple(param_types or ()),
        )

    @staticmethod
    def get_signature_key(node, ftype, func_name, is_static, declarator=None):
        is_function_like = ftype in ["func", "func_decl"]
        return_type = TS.get_return_type(node) if is_function_like else ""
        param_types = TS.get_param_types(node, is_function_like, declarator=declarator)
        return TS.make_signature_key(ftype, func_name, return_type, is_static, param_types)

    @staticmethod
    def key_matches(key, ftype=None, name=None):
        if not isinstance(key, tuple) or len(key) < 2:
            return False
        if ftype is not None and key[0] != ftype:
            return False
        if name is not None and key[1] != name:
            return False
        return True

    @staticmethod
    def determine_type_and_attributes(node):
        ftype = None
        is_function = False
        is_static = False
        if node.type == "function_definition":
            ftype = "func"
            is_function = True
        elif node.type == "declaration":
            if TS.is_extern_decl(node):
                ftype = "extern"
            else:
                _declarator = TS.find_declarator(node)
                if _declarator is None:
                    ftype = "decl"
                if _declarator.type == "function_declarator":
                    ftype = "func_decl"
                elif _declarator.type == "pointer_declarator":
                    while _declarator.type == "pointer_declarator":
                        _declarator = TS.find_declarator(_declarator)
                    if _declarator.type == "function_declarator":
                        ftype = "func_decl"
                    else:
                        ftype = "decl"
                else:
                    # assignments, initializations, etc.
                    ftype = "decl"
        elif node.type == "struct_specifier":
            ftype = "struct"
        elif node.type == "enum_specifier":
            ftype = "enum"
        elif node.type == "union_specifier":
            ftype = "union"
        elif node.type == "type_definition":
            ftype = "typedef"
        elif node.type == "macro_type_specifier":
            ftype = "macro"
        for _child in node.children:
            if _child.type == "storage_class_specifier":
                if "static" == _child.text.decode('utf-8'):
                    is_static = True
        return ftype, is_function, is_static

    @staticmethod
    def get_pos(point):
        return [point.row, point.column]
    
    @staticmethod
    def is_extern_decl(node):
        for child in node.children:
            if child.type == "storage_class_specifier":
                if child.text.decode('utf-8').strip() == "extern":
                    return True
        return False
    
    @staticmethod
    def is_static_decl(node):
        for child in node.children:
            if child.type == "storage_class_specifier":
                if "static" == child.text.decode('utf-8'):
                    return True
        return False

    @staticmethod
    def get_instances(node, instance_type):
        instances = []
        for child in node.children:
            if child.type == instance_type:
                instances.append(child)
            instances.extend(TS.get_instances(child, instance_type))
        return instances

    @staticmethod
    def get_parameters(node, is_function):
        parameters = []
        if not is_function:
            return parameters
        declarator = TS.find_declarator(node)
        while declarator.type == "pointer_declarator":
            declarator = declarator.child_by_field_name('declarator')
        if declarator.child_by_field_name('parameters') is None:
            return parameters
        parameter_list = declarator.child_by_field_name('parameters').named_children
        for param in parameter_list:
            if param.type != "parameter_declaration":
                continue
            param_info = {}
            param_info["parameter"] = param.text.decode('utf-8') + ";" + str(param.start_point.row)
            if len(param.children) == 1:
                _type = param.children[0].text.decode('utf-8')
                _name = ""
            else:
                *_type_node, _name_node = param.children
                _type = ""
                for _type_part in _type_node:
                    _type += _type_part.text.decode('utf-8')
                _name = TS.get_name(_name_node)
            param_info["param_type"] = _type
            param_info["param_name"] = _name
            param_info["function_ptr"] = is_function
            parameters.append(param_info)
        return parameters
    
    @staticmethod
    def get_all_types(test_string):
        all_types = []
        tree = TS.get_tree(test_string)
        for node in TS.traverse_tree(tree):
            if node.type in ["primitive_type", "type_identifier"]:
                all_types.append(TS.get_node_text(node))
        return all_types
        
class UnitTS:
    def __init__(self, target_file_path: str):
        self.target_file_path = target_file_path

    def _refresh(self):
        self.tree = TS.parse_file(self.target_file_path)
        with open(self.target_file_path, 'r', encoding='utf-8', errors='replace') as f:
            self.code = f.read()

    def _update(self):
        with open(self.target_file_path, 'w', encoding='utf-8') as f:
            f.write(self.code)
        self.tree = TS.parse_file(self.target_file_path)

    def _trimmer(self, node, lineno, marked_ranges_for_deletion):
        if lineno is None:
            return
        for child in node.children:
            self._trimmer(child, lineno, marked_ranges_for_deletion)
            if child.start_point.row <= lineno:
                continue
            line_or_block = child.text.decode('utf-8').strip()
            if line_or_block.endswith(';') or (line_or_block.endswith('}') and not line_or_block.startswith('{')):
                if line_or_block.startswith(';') or line_or_block.startswith('}'):
                    continue
                if child.type == "labeled_statement":
                    # TODO: This still removes logic and we need to use control flow
                    continue

                # check if there is any labeled_statement in block as a nested child
                # if so, then do not remove the block
                _labeled_statement = TS.get_instances(child, "labeled_statement")
                if _labeled_statement:
                    continue

                # check if inside a labeled statement
                _current_node = child
                while _current_node.parent and _current_node.type != "labeled_statement":
                    _current_node = _current_node.parent
                if _current_node.type == "labeled_statement":
                    continue

                marked_ranges_for_deletion.append((child.start_point, child.end_point))

    def _dropper(self, node, func_name, ftype, drop_set, marked_ranges_for_deletion, **kwargs):
        if node.type == "declaration":
            # check for multiple variables declared in the same line
            declarators = node.children_by_field_name('declarator')
            if len(declarators) <= 1:
                pass
            else:
                for declarator in declarators:
                    _func_name = TS.get_name(declarator)
                    _next_named_sibling = declarator.next_named_sibling
                    _drop_key = TS.get_signature_key(node, ftype, _func_name, TS.is_static_decl(node), declarator=declarator)
                    if (ftype, _func_name) in drop_set or _drop_key in drop_set:
                        _node_to_delete = declarator
                        while _next_named_sibling and _node_to_delete != _next_named_sibling:
                            marked_ranges_for_deletion.append((_node_to_delete.start_point, _node_to_delete.end_point))
                            _node_to_delete = _node_to_delete.next_sibling
                return
        node_key = TS.get_signature_key(node, ftype, func_name, TS.is_static_decl(node))
        if (ftype, func_name) in drop_set or node_key in drop_set:
            marked_ranges_for_deletion.append((node.start_point, node.end_point))

    def _proc_trimmer(self, node, func_name, ftype, marked_ranges_for_deletion, required_func, lineno,  **kwargs):
        if lineno and required_func: 
            node_key = TS.get_signature_key(node, ftype, func_name, TS.is_static_decl(node))
            if (ftype, func_name) == required_func or node_key == required_func:
                # trim the function removing all blocks after the required line
                # after marking lineeno has shifted by 1
                # also want to keep the post comment so shift by 2
                target_comment_node = TS.track_comment(self.tree, "/*target_line*/")
                _next_sibling = target_comment_node.next_named_sibling
                if _next_sibling != node:
                    parent_statement = TS.get_parent_statement(target_comment_node)
                    assert parent_statement is not None
                    lineno = parent_statement.end_point.row
                    if "clang-format on" not in parent_statement.text.decode('utf-8'):
                        lineno += 1
                else:
                    # only body content should be trimmed if it is past the target line
                    _body = node.child_by_field_name("body")
                    if _body is None or _body.named_child_count == 0:
                        lineno = None
                    _body_line = _body.named_children[0].start_point.row
                    if _body_line > lineno:
                        lineno = _body_line
                self._trimmer(node, lineno, marked_ranges_for_deletion)
        if (ftype, func_name) == ("func", "main"):
            logger.warning("Renamed main to original_main at {}", self.target_file_path)
            declarator = TS.find_declarator(node)
            _original = TS.get_node_text(declarator)
            _replacement = _original.replace("main", "original_main")
            self.code = self.code.replace(_original, _replacement, 1)
    
    @affects_tree
    def mark(self, line_no: int):
        _pre = '// clang-format off'
        _insert = '/*target_line*/'
        _post = '// clang-format on'
        
        lines = self.code.splitlines()
        if line_no < 0 or line_no >= len(lines):
            raise ValueError("Invalid line number")
        
        lines.insert(line_no -1, _pre)
        lines[line_no] = _insert + lines[line_no].strip()
        lines.insert(line_no + 1, _post)

        self.code = '\n'.join(lines)
        
    @affects_tree
    def trim(self, required_func, lineno):
        marked_ranges_for_deletion = []
        for node in self.tree.root_node.children:
            TS.process_node(node, self.target_file_path, self._proc_trimmer, {
                "marked_ranges_for_deletion": marked_ranges_for_deletion,
                "lineno": lineno,
                "required_func": required_func
            })
        
        self.code = TS._remove_marked_ranges(marked_ranges_for_deletion, self.code)


    @affects_tree
    def drop(self, drop_set, used):
        _ = used
        marked_ranges_for_deletion = []
        for node in self.tree.root_node.children:
            last_error_node = None
            if node.type == "ERROR":
                logger.warning("Error in parsing {}", self.target_file_path)
                if last_error_node is None:
                    last_error_node = node
                else:
                    marked_ranges_for_deletion.append((last_error_node.start_point, node.end_point))
                    last_error_node = None
            if last_error_node:
                marked_ranges_for_deletion.append((last_error_node.start_point, last_error_node.end_point))
            TS.process_node(node, self.target_file_path, self._dropper, {
                "drop_set": drop_set,
                "marked_ranges_for_deletion": marked_ranges_for_deletion,
            })
        
        self.code = TS._remove_marked_ranges(marked_ranges_for_deletion, self.code)

    def get_names_in_funcs(self):
        # names_in_funcs =  ["".join(node.itertext()) for node in unit_srcml.nxml(".//src:function//src:name")]
        names_in_funcs = []
        for node in TS.traverse_tree(self.tree):
            if len(node.children) == 0 and "identifier" in node.type:
                _identifier_name = TS.get_node_text(node)
                _parent_statement = TS.get_parent_statement(node)
                if _parent_statement and _parent_statement.type in ["struct_specifier", "enum_specifier", "union_specifier"]:
                    continue
                elif _parent_statement and _parent_statement.type == "function_definition":
                    names_in_funcs.append(_identifier_name)
                else:
                    _parent = node.parent
                    while _parent and _parent.type != "function_definition":
                        _parent = _parent.parent
                    if _parent:
                        names_in_funcs.append(_identifier_name)
        return names_in_funcs
    
    def get_globals(self):
        # globals_in_file = [(srcml.get_name(node), "".join(node.itertext()), srcml.get_instances(node, ".//src:type")) for node in unit_srcml.nxml("./src:decl_stmt/src:decl")]
        globals_in_file = {}
        externs_in_file = {}
        for node in self.tree.root_node.children:
            if node.type == "declaration":
                _name = TS.get_name(node)
                _text = TS.get_node_text(node)
                # _type_node = node.child_by_field_name('type')
                # if _type_node is None:
                #     _type = ""
                # else:
                #     _type = TS.get_node_text(_type_node)
                
                # note that this ignores primitive types
                # _types_used = TS.collect_non_primitive_type_identifiers(node)

                declarators = node.children_by_field_name('declarator')
                func_names = [
                    TS.get_name(declarator) for declarator in declarators
                ]
                if not func_names:
                    func_names = [_name]
                if TS.is_extern_decl(node):
                    for _name in func_names:
                        externs_in_file[_name] = _text
                else:
                    for _name in func_names:
                        globals_in_file[_name] = _text
        return globals_in_file, externs_in_file

