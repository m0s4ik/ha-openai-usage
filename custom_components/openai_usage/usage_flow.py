"""Options flow of the main entry: price overrides (openai_usage addition)."""

import json
from typing import Any

import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .prices import PriceError, validate_overrides
from .usage import CONF_PRICES

EXAMPLE = '{"gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.6}}'


class OpenAIUsageOptionsFlow(OptionsFlow):
    """Edit the price overrides of the usage tracking."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the price override table."""
        errors: dict[str, str] = {}
        placeholders = {"example": EXAMPLE, "model": "", "key": "", "error": ""}
        if user_input is not None:
            text = (user_input.get(CONF_PRICES) or "").strip()
            try:
                overrides = validate_overrides(json.loads(text)) if text else {}
            except PriceError as err:
                errors[CONF_PRICES] = err.reason
                placeholders.update(err.placeholders)
            except (ValueError, RecursionError) as err:
                # JSONDecodeError (with line and column), huge integers, or
                # nesting too deep for the parser.
                errors[CONF_PRICES] = "invalid_json"
                placeholders["error"] = str(err) or type(err).__name__
            else:
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_PRICES: overrides}
                )
            current = text
        else:
            overrides = self.config_entry.options.get(CONF_PRICES) or {}
            current = json.dumps(overrides, indent=2) if overrides else ""

        schema = probatio.Schema(
            {
                probatio.Optional(
                    CONF_PRICES, description={"suggested_value": current}
                ): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )


@callback
def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
    """Return the options flow handler."""
    return OpenAIUsageOptionsFlow()


def attach(flow_class: type[ConfigFlow]) -> None:
    """Give the (upstream) config flow class our options flow."""
    flow_class.async_get_options_flow = staticmethod(async_get_options_flow)  # type: ignore[method-assign]
