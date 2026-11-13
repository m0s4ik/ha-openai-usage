# OpenAI with Token & Cost Tracking

[<img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-m0s4ik-FFDD00?logo=buymeacoffee&logoColor=black">](https://buymeacoffee.com/m0s4ik)

A copy of Home Assistant's official **OpenAI** integration (`openai_conversation`,
Home Assistant 2026.11) that also tells you **how many tokens each request used and
what it cost**. It installs as a custom integration (domain `openai_usage`) and
runs side by side with the official one.

Everything the official integration does is still there, unchanged: conversation
agent with tools and the Assist API, AI tasks (data, images, evaluation), speech-to-text,
text-to-speech, the same config and subentry flows and options, English and Italian
translations. On top of that:

- an **`openai_usage` event** for every OpenAI request, with the model, the entity
  and subentry that made it, the tokens (input, cached input, output, reasoning),
  audio seconds or characters, and the cost in USD;
- a **usage device** per config entry with a few sensors: cost today, cost this
  month, tokens today, requests today (and, disabled by default, cost today per kind:
  conversation, AI task, speech-to-text, text-to-speech);
- a **price table** for the models the integration offers by default, which you can
  override from the integration options.

## Why

OpenAI's dashboard shows the cost per API key and per day, but not which agent,
pipeline or automation spent it. The official integration only puts the token
counts in the conversation debug trace. This copy reads the `usage` the API already
returns with every response, so you can see the cost of each request in Home
Assistant, keep a budget with an automation, or compare models.

## Installation

Requirements: Home Assistant 2026.11.0 or newer, with HACS (the Marketplace).

1. Open HACS, menu (three dots) → **Custom repositories**, add
   `https://github.com/m0s4ik/ha-openai-usage` with type **Integration**, or use the
   link:
   [open in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=m0s4ik&repository=ha-openai-usage&category=integration).
2. Download **OpenAI with Token & Cost Tracking** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *OpenAI with Token & Cost Tracking*, and
   enter an OpenAI API key (the same key as the official integration is fine).

It creates the same subentries as the official integration: a conversation agent,
an AI task, an evaluation task, a speech-to-text and a text-to-speech entity, plus the
usage device.

### Side by side with the official integration

The two integrations have different domains and do not share anything: you can keep
the official one configured and move one pipeline at a time. Entity names are the
same as the official ones (for example *OpenAI Conversation*); Home Assistant adds
a suffix (`conversation.openai_conversation_2`) if the official entities already exist,
and you can rename them.

### Switching a voice pipeline (e.g. Alfred) or an AI task

- **Assist pipeline** (any satellite, including the pipeline of a voice assistant such
  as *Alfred*): Settings → Voice assistants → open the pipeline → choose the
  conversation agent, speech-to-text and text-to-speech provided by *OpenAI with
  Token & Cost Tracking*. Settings that belong to the agent (prompt, model, tools) are in the
  subentry of this integration, not of the official one.
- **AI tasks**: Settings → System → General → AI suggestions (default AI task entity),
  or pass the entity of this integration to `ai_task.generate_data`.

## Usage data

### Event `openai_usage`

Fired once per OpenAI API request (a conversation turn that calls tools makes several
requests):

```yaml
event_type: openai_usage
data:
  config_entry_id: 01J...
  entity_id: conversation.openai_conversation
  subentry_id: 01J...
  kind: conversation        # conversation | ai_task | stt | tts
  model: gpt-4o-mini-2024-07-18
  input_tokens: 1500
  cached_input_tokens: 1024
  output_tokens: 42
  reasoning_tokens: 0
  audio_seconds: 0.0
  characters: 0
  cost_usd: 0.0001734        # null when the model has no price (see Prices)
```

Where the numbers come from:

| Kind | Source |
| --- | --- |
| `conversation`, `ai_task` (data, image) | `usage` of the Responses API (`response.completed`, `incomplete` or `failed` event): input, cached input, output and reasoning tokens. The model is the one OpenAI reports. |
| `ai_task` (evaluation) | `usage` of the Decisions API response. |
| `stt` | `usage` of the transcription when present (tokens, with audio tokens, or seconds); the audio duration is computed from the PCM audio sent. |
| `tts` | The speech API returns no usage: the number of characters of the text is recorded. |

### Sensors

| Sensor | Unit | Notes |
| --- | --- | --- |
| Cost today | USD | `state_class: total`, resets at local midnight |
| Cost this month | USD | resets at local midnight of the 1st |
| Tokens today | tokens | input + output tokens |
| Requests today | requests | |
| Unpriced requests today | requests | diagnostic: requests whose cost is unknown (see Prices) |
| Conversation / AI task / Speech-to-text / Text-to-speech cost today | USD | disabled by default |

Values are restored after a restart if the day (or month) has not changed.

The device is named after the config entry: *ChatGPT with Token & Cost Tracking
usage* by default (*Consumi di …* in Italian). Home Assistant builds the entity IDs
from the device name in its language when the entities are first created; in
English they are `sensor.chatgpt_with_token_cost_tracking_usage_cost_today` and so
on (check yours in the device page; you can rename them).

Example: a notification when the daily cost goes over one dollar:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.chatgpt_with_token_cost_tracking_usage_cost_today
    above: 1
actions:
  - action: notify.notify
    data:
      message: "OpenAI spending today: {{ states('sensor.chatgpt_with_token_cost_tracking_usage_cost_today') }} USD"
```

### Prices

The built-in table is in [`prices.py`](custom_components/openai_usage/prices.py)
(USD; per 1M tokens for `input`, `cached_input`, `output`, `audio_input`; per minute of
audio for `per_minute`; per 1M characters for `per_1m_characters`). These are list
prices (standard tier, short context) checked on 13 November 2026, **not fetched from
OpenAI**: check [OpenAI's pricing page](https://developers.openai.com/api/docs/pricing)
and override what changed.

Dated model names match their base model (`gpt-4o-mini-2024-07-18` uses
`gpt-4o-mini`). Only a date suffix is ignored: related models such as `gpt-5-pro`,
`o3-mini` or `gpt-4o-search-preview` have their own prices and never fall back to
`gpt-5`, `o3` or `gpt-4o`.

A request whose cost cannot be computed (a model that is not in the table, or a
price without the key the request needs, e.g. a speech model without
`per_1m_characters`) is **not counted as free**: its event has `cost_usd: null`, the
cost sensors leave it out, *Unpriced requests today* counts it, and a repair issue
in Settings → System → Repairs names the model. Add its price in the options and the
issue goes away when the integration reloads.

To override or add prices: Settings → Devices & services → *OpenAI with Token & Cost Tracking* →
**Configure**, and write a JSON object; only the models and keys you write change
(prices must be numbers, 0 or more; the form says which model and key is wrong):

```json
{
  "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.6},
  "my-new-model": {"input": 1.0, "output": 4.0}
}
```

Not priced: batch/flex/priority tiers, web search and code interpreter tool calls,
image generation (the image tool is billed apart from the response's token usage).
For `gpt-4o-mini-tts` the cost is an estimate from the characters.

## Privacy

Nothing leaves Home Assistant except the calls to OpenAI that the official
integration makes anyway. Usage numbers are computed locally from the API responses,
kept in Home Assistant's state machine and recorder, and fired as local events.

## Keeping up with the official integration

The copied files differ from upstream only in lines marked `# openai_usage:` and in
the manifest and translations; our code is in `usage.py`, `sensor.py`, `prices.py`
and `usage_flow.py`. `upstream/` holds the untouched official files, and
[`scripts/sync.py`](scripts/sync.py) builds the integration and the tests from them
by applying the hooks it lists.

To move to a newer Home Assistant release:

```shell
scripts/resync.sh 2026.12.0      # downloads the official files, shows what changed,
                                 # reapplies the hooks (fails naming any hook that broke)
python -m pytest                 # with requirements_test.txt installed
```

CI checks that the generated files match `upstream/` plus the hooks, runs hassfest
and the HACS validation, and runs the official tests (adapted) plus the usage tests.

If this integration saved you some time, you can also
[buy me a coffee ☕](https://buymeacoffee.com/m0s4ik): it keeps the next one coming.

## License

Apache License 2.0, like Home Assistant. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Not affiliated with or endorsed by OpenAI, Home Assistant, Nabu Casa or the Open Home
Foundation.
