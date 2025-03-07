# Copyright (c) Microsoft. All rights reserved.

import logging
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

from pydantic import Field, field_validator, model_validator

from semantic_kernel.functions.kernel_function_metadata import KernelFunctionMetadata
from semantic_kernel.functions.kernel_plugin_collection import KernelPluginCollection
from semantic_kernel.template_engine.blocks.block import Block
from semantic_kernel.template_engine.blocks.block_errors import CodeBlockRenderError, CodeBlockTokenError
from semantic_kernel.template_engine.blocks.block_types import BlockTypes
from semantic_kernel.template_engine.blocks.function_id_block import FunctionIdBlock
from semantic_kernel.template_engine.code_tokenizer import CodeTokenizer

if TYPE_CHECKING:
    from semantic_kernel.functions.kernel_arguments import KernelArguments
    from semantic_kernel.functions.kernel_function import KernelFunction
    from semantic_kernel.kernel import Kernel

logger: logging.Logger = logging.getLogger(__name__)

VALID_ARG_TYPES = [BlockTypes.VALUE, BlockTypes.VARIABLE, BlockTypes.NAMED_ARG]


class CodeBlock(Block):
    """Create a code block.

    A code block is a block that usually contains functions to be executed by the kernel.
    It consists of a list of tokens that can be either a function_id, value, a variable or a named argument.

    If the first token is not a function_id but a variable or value, the rest of the tokens will be ignored.
    Only the first argument for the function can be a variable or value, the rest of the tokens have be named arguments.

    Args:
        content: The content of the code block.
        tokens: The list of tokens that compose the code block, if empty, will be created by the CodeTokenizer.

    Raises:
        CodeBlockTokenError: If the content does not contain at least one token.
        CodeBlockTokenError: If the first token is a named argument.
        CodeBlockTokenError: If the second token is not a value or variable.
        CodeBlockTokenError: If a token is not a named argument after the second token.
        CodeBlockRenderError: If the plugin collection is not set in the kernel.
        CodeBlockRenderError: If the function is not found in the plugin collection.
        CodeBlockRenderError: If the function does not take any arguments but it is being
            called in the template with arguments.
    """

    type: ClassVar[BlockTypes] = BlockTypes.CODE
    tokens: List[Block] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def parse_content(cls, fields: Any) -> Any:
        """Parse the content of the code block and tokenize it.

        If tokens are already present, skip the tokenizing.
        """
        if isinstance(fields, Block) or "tokens" in fields:
            return fields
        content = fields.get("content", "").strip()
        fields["tokens"] = CodeTokenizer.tokenize(content)
        return fields

    @field_validator("tokens", mode="after")
    def check_tokens(cls, tokens: List[Block]) -> List[Block]:
        """Check the tokens in the list.

        If the first token is a value or variable, the rest of the tokens will be ignored.
        If the first token is a function_id, then the next token can be a value,
            variable or named_arg, the rest have to be named_args.

        Raises:
            CodeBlockTokenError: If the content does not contain at least one token.
            CodeBlockTokenError: If the first token is a named argument.
            CodeBlockTokenError: If the second token is not a value or variable.
            CodeBlockTokenError: If a token is not a named argument after the second token.
        """
        if not tokens:
            raise CodeBlockTokenError("The content should contain at least one token.")
        for index, token in enumerate(tokens):
            if index == 0 and token.type == BlockTypes.NAMED_ARG:
                raise CodeBlockTokenError(
                    f"The first token needs to be a function_id, value or variable, got: {token.type}"
                )
            if index == 0 and token.type in [BlockTypes.VALUE, BlockTypes.VARIABLE]:
                if len(tokens) > 1:
                    logger.warning(
                        "The first token is a value or variable, but there are more tokens in the content, \
these will be ignored."
                    )
                return [token]
            if index == 1 and token.type not in VALID_ARG_TYPES:
                raise CodeBlockTokenError(
                    f"Unexpected type for the second token type, should be variable, value or named_arg: {token.type}"
                )
            if index > 1 and token.type != BlockTypes.NAMED_ARG:
                raise CodeBlockTokenError(
                    f"Every argument for the function after the first has to be a named arg, instead: {token.type}"
                )
        return tokens

    async def render_code(self, kernel: "Kernel", arguments: "KernelArguments") -> str:
        """Render the code block.

        If the first token is a function_id, it will call the function from the plugin collection.
        Otherwise it is a value or variable and those are then rendered directly.
        """
        logger.debug(f"Rendering code: `{self.content}`")
        if self.tokens[0].type == BlockTypes.FUNCTION_ID:
            return await self._render_function_call(kernel, arguments)
        # validated that if the first token is not a function_id, it is a value or variable
        return self.tokens[0].render(kernel, arguments)

    async def _render_function_call(self, kernel: "Kernel", arguments: "KernelArguments"):
        if not kernel.plugins:
            raise CodeBlockRenderError("Plugin collection not set in kernel")
        function_block = self.tokens[0]
        function = self._get_function_from_plugin_collection(kernel.plugins, function_block)
        if not function:
            error_msg = f"Function `{function_block.content}` not found"
            logger.error(error_msg)
            raise CodeBlockRenderError(error_msg)

        arguments_clone = copy(arguments)
        if len(self.tokens) > 1:
            arguments_clone = self._enrich_function_arguments(kernel, arguments_clone, function.metadata)

        result = await function.invoke(kernel, arguments_clone)
        if exc := result.metadata.get("error", None):
            raise CodeBlockRenderError(f"Error rendering function: {function.metadata} with error: {exc}") from exc

        return str(result) if result else ""

    def _enrich_function_arguments(
        self,
        kernel: "Kernel",
        arguments: "KernelArguments",
        function_metadata: KernelFunctionMetadata,
    ) -> "KernelArguments":
        if not function_metadata.parameters:
            raise CodeBlockRenderError(
                f"Function {function_metadata.plugin_name}.{function_metadata.name} does not take any arguments "
                f"but it is being called in the template with {len(self.tokens) - 1} arguments."
            )
        for index, token in enumerate(self.tokens[1:], start=1):
            logger.debug(f"Parsing variable/value: `{self.tokens[1].content}`")
            rendered_value = token.render(kernel, arguments)
            if token.type != BlockTypes.NAMED_ARG and index == 1:
                arguments[function_metadata.parameters[0].name] = rendered_value
                continue
            arguments[token.name] = rendered_value

        return arguments

    def _get_function_from_plugin_collection(
        self, plugins: KernelPluginCollection, function_block: FunctionIdBlock
    ) -> Optional["KernelFunction"]:
        """
        Get the function from the plugin collection

        Args:
            plugins: The plugin collection
            function_block: The function block that contains the function name

        Returns:
            The function if it exists, None otherwise.
        """
        if function_block.plugin_name is not None and len(function_block.plugin_name) > 0:
            return plugins[function_block.plugin_name][function_block.function_name]
        else:
            # We now require a plug-in name, but if one isn't set then we'll try to find the function
            for plugin in plugins:
                if function_block.function_name in plugin:
                    return plugin[function_block.function_name]

        return None
