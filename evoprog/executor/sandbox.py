"""
evoprog.executor.sandbox: AST 白名单验证 + 受限 exec 沙箱 + multiprocessing 超时。

安全设计原则：
- 白名单方式（非黑名单）：只允许特定标识符，拒绝其他一切
- 禁止所有属性访问（防止 .__class__.__subclasses__() 逃逸）
- exec 的 __builtins__ 设为白名单 dict（防止 Pitfall 1）
- multiprocessing 超时后 terminate() + join()（防止 Pitfall 3 僵尸进程）
"""

from __future__ import annotations

import ast
import multiprocessing
from typing import Optional

from evoprog.config import ALLOWED_NAMES, SAFE_BUILTINS


class ValidationError(Exception):
    """AST 验证错误：包含错误类型、位置信息和违规变量名。"""

    def __init__(
        self,
        message: str,
        lineno: int = 0,
        col: int = 0,
        name: str = '',
        error_type: str = 'forbidden_access',
    ):
        super().__init__(message)
        self.lineno = lineno
        self.col = col
        self.forbidden_name = name  # 违规变量名
        self.error_type = error_type  # 'syntax_error' | 'forbidden_access'

    def __repr__(self) -> str:
        return (
            f"ValidationError(error_type={self.error_type!r}, "
            f"lineno={self.lineno}, name={self.forbidden_name!r}, "
            f"msg={str(self)!r})"
        )


class CodeValidator(ast.NodeVisitor):
    """
    AST 白名单验证器。

    验证策略：
    - Name 节点：只允许 ALLOWED_NAMES 中的标识符
    - Import/ImportFrom 节点：全部拒绝
    - Attribute 节点：全部拒绝（防止链式属性访问逃逸）
    - Global/Nonlocal 节点：全部拒绝
    - Delete 节点：全部拒绝
    """

    def __init__(self, allowed_names: frozenset = ALLOWED_NAMES):
        self.allowed_names = allowed_names
        self.errors: list[ValidationError] = []

    def visit_Name(self, node: ast.Name) -> None:
        """检查变量名是否在白名单中。"""
        if node.id not in self.allowed_names:
            self.errors.append(ValidationError(
                f"禁止访问变量: '{node.id}'",
                lineno=node.lineno,
                col=node.col_offset,
                name=node.id,
                error_type='forbidden_access',
            ))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """拒绝所有 import 语句。"""
        names = ', '.join(alias.name for alias in node.names)
        self.errors.append(ValidationError(
            f"禁止 import 语句: import {names}",
            lineno=node.lineno,
            error_type='forbidden_access',
        ))
        # 不调用 generic_visit，避免子节点产生重复错误

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """拒绝所有 from...import 语句。"""
        self.errors.append(ValidationError(
            f"禁止 from...import 语句: from {node.module} import ...",
            lineno=node.lineno,
            error_type='forbidden_access',
        ))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """
        拒绝所有属性访问（如 os.system、obj.__class__）。

        策略代码只需访问数组索引（value[0]、obs[key]），不需要任何属性访问。
        拒绝属性访问可防止通过字面量对象链式调用（如
        ().__class__.__bases__[0].__subclasses__()）绕过 Name 检查。
        """
        self.errors.append(ValidationError(
            f"禁止属性访问: '.{node.attr}'",
            lineno=node.lineno,
            col=node.col_offset,
            error_type='forbidden_access',
        ))
        # 不调用 generic_visit，避免子节点产生重复错误

    def visit_Global(self, node: ast.Global) -> None:
        """拒绝 global 语句。"""
        names = ', '.join(node.names)
        self.errors.append(ValidationError(
            f"禁止 global 语句: global {names}",
            lineno=node.lineno,
            error_type='forbidden_access',
        ))

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        """拒绝 nonlocal 语句。"""
        names = ', '.join(node.names)
        self.errors.append(ValidationError(
            f"禁止 nonlocal 语句: nonlocal {names}",
            lineno=node.lineno,
            error_type='forbidden_access',
        ))

    def visit_Delete(self, node: ast.Delete) -> None:
        """拒绝 del 语句。"""
        self.errors.append(ValidationError(
            f"禁止 del 语句",
            lineno=node.lineno,
            error_type='forbidden_access',
        ))


def validate_code(code: str) -> list[ValidationError]:
    """
    对策略代码进行 AST 白名单验证。

    Returns:
        空列表表示验证通过；非空列表包含所有验证错误。
    """
    try:
        tree = ast.parse(code, mode='exec')
    except SyntaxError as e:
        return [ValidationError(
            f"语法错误: {e.msg}",
            lineno=e.lineno or 0,
            col=e.offset or 0,
            error_type='syntax_error',
        )]

    validator = CodeValidator(ALLOWED_NAMES)
    validator.visit(tree)
    return validator.errors


def _run_code_in_subprocess(
    code: str,
    local_vars: dict,
    result_queue: 'multiprocessing.Queue',
) -> None:
    """
    在子进程中执行策略代码，结果通过 Queue 传回主进程。

    该函数在独立子进程中运行，隔离执行环境。
    """
    try:
        # 复制 SAFE_BUILTINS（防止子进程修改影响其他进程）
        exec_globals = dict(SAFE_BUILTINS)
        exec_locals = dict(local_vars)
        exec(compile(code, '<strategy>', 'exec'), exec_globals, exec_locals)
        # 返回修改后的 value（保持列表引用语义）
        result_queue.put(('ok', exec_locals.get('value', [0.0])))
    except Exception as e:
        result_queue.put(('error', str(e)))


def execute_code_with_timeout(
    code: str,
    local_vars: dict,
    timeout_seconds: float = 2.0,
) -> tuple[str, object]:
    """
    在独立子进程中执行策略代码，带超时保护。

    Returns:
        ('ok', value_list)    - 执行成功
        ('error', message)    - 运行时错误
        ('timeout', None)     - 执行超时，子进程已终止
    """
    ctx = multiprocessing.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(
        target=_run_code_in_subprocess,
        args=(code, local_vars, q),
    )
    p.start()
    p.join(timeout=timeout_seconds)

    if p.is_alive():
        # 超时：终止子进程，防止僵尸进程（Pitfall 3）
        p.terminate()
        p.join()
        return ('timeout', None)

    # 子进程正常结束，读取结果
    if not q.empty():
        return q.get_nowait()

    # 子进程异常退出但没有放结果（如被 OOM killer）
    return ('error', f'子进程异常退出（exit code: {p.exitcode}）')
