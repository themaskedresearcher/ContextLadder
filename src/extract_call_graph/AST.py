import enum
from functools import total_ordering

@total_ordering
class DataType(enum.Enum):
    BUILTIN = 0  # All basic types: int, float, double, ...
    STRING = 1  # char *, const char *
    ENUM = 2
    ARRAY = 3
    VOIDP = 4
    QUALIFIER = 5  # const, volatile, and restrict qualifiers
    POINTER = 6
    STRUCT = 7
    INCOMPLETE = 8
    FUNCTION = 9
    INPUTFILE = 10
    OUTPUTFILE = 11
    UNKNOWN = 12

    def __lt__(self, other):
        if self.__class__ is other.__class__:
            return self.value < other.value
        return NotImplemented


class Function:
    def __init__(
        self,
        name: str,
        file_name: str,
        ftype: str,
        start: list,
        end: list,
        is_function: bool,
        is_static: bool,
        return_type: str = "",
        param_types: tuple = (),
        signature_key: tuple = None
    ):
        self.name = name
        self.file_name = file_name
        self.type = ftype
        self.start = start
        self.end = end
        
        self.labels = None
        self.calls = None
        self.types_used = None
        self.parameter = None
        self.expr_used = None
        
        self.is_function = is_function
        self.function_ptrs = []
        self.is_static = is_static

        # Signature-aware identity fields. These let callers distinguish
        # overloaded C++ functions that share the same name and file.
        self.return_type = return_type or ""
        self.param_types = tuple(param_types or ())
        self.signature_key = signature_key
    
    def construct_params(self, name, file_name, srcmlparams):
        parameters = []
        # if name == "original_main":
        #     name = "main"
        # for param in node.xpath("./src:parameter_list/src:parameter/src:decl", namespaces=ns):
        #     params.append({
        #         "param_name": get_name(param),
        #         "param_type": get_type(param, ""),
        #         # "generator_type":
        #         # "array_size":
        #         # "parent_type":
        #         # "parent_gen":
        #         # "param_usage":
        #     })

        for i, param in enumerate(srcmlparams):
            # Do not allow variable argument to be counted as argument
            if srcmlparams[i]["parameter"].split(";")[0] == "...":
                continue
            parameters.append(
                {
                    "parameter": srcmlparams[i]["parameter"].split(";")[0],
                    "param_name": srcmlparams[i]["param_name"],
                    "param_type": srcmlparams[i]["param_type"],
                    "function_ptr": srcmlparams[i]["function_ptr"],
                    "generator_type": DataType.UNKNOWN,
                    "param_usage": "UNKNOWN",
                }
            )
        return parameters

    def params(self, srcmlparams):
        if self.is_function:
            return self.construct_params(self.name, self.file_name, srcmlparams)
        else:
            return []

    def encloses(self, line: int) -> bool:
        # Ignores column numbers
        if self.start[0] <= line <= self.end[0]:
            return True
        else:
            return False


