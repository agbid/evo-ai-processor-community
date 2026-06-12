# Integração nativa do Google Calendar (OAuth) — histórico completo

Este documento registra **tudo** o que foi feito para colocar a integração nativa
(OAuth) do Google Calendar funcionando de ponta a ponta no agente de IA, em
produção (`intercrm.gestordeleads.com` / `apievocrm.gestordeleads.com`).

A integração permite que o agente:
- Mostre o status "ATIVO" na aba **Integrações** do agente após o usuário
  autorizar a conta Google;
- Liste os calendários do Google da conta conectada para o usuário escolher;
- Salve a configuração (calendário selecionado, horários de atendimento,
  antecedência mínima, duração máxima, etc.);
- Use as tools `check_calendar_availability` (verificar disponibilidade /
  buscar horários livres), `create_calendar_event` (criar evento, com
  lembretes nativos via `reminder_minutes_before`), `update_calendar_event`
  (reagendar/editar) e `delete_calendar_event` (cancelar) durante a conversa.

## Repositórios e branches envolvidos

| Repositório | Fork usado para deploy | Branch | Imagem publicada |
|---|---|---|---|
| `evo-ai-processor-community` | `agbid/evo-ai-processor-community` | `production` | `ghcr.io/agbid/evo-ai-processor-community:latest` |
| `evo-ai-frontend-community` | `agbid/evo-ai-frontend-community` | `production` | `ghcr.io/agbid/evo-ai-frontend-community:latest` |

**Processo de deploy** (usado para cada um dos fixes abaixo):
1. Commit na branch `production` do fork (`agbid`).
2. `git push agbid production` → dispara o workflow *Build & Publish (Fork) Image*
   no GitHub Actions (build multi-arch amd64/arm64 + merge de manifesto).
3. Após o build terminar com sucesso, no servidor de produção
   (`213.199.37.111`, Docker Swarm):
   - Backend: `docker service update --force --image ghcr.io/agbid/evo-ai-processor-community:latest evocrm_evocrm_processor`
   - Frontend: `docker service update --force --image ghcr.io/agbid/evo-ai-frontend-community:latest evocrm_evocrm_frontend`
4. Aguardar `Service ... converged` e validar via Playwright em produção.

---

## Linha do tempo dos problemas e correções

A integração não funcionava por **uma cadeia de bugs independentes** em
camadas diferentes (middleware de auth, serviços globais de credenciais,
callback OAuth, persistência da config do agente, normalização de resposta no
frontend, e por fim a lógica de horários de atendimento das tools). Cada item
abaixo só pôde ser detectado depois que o anterior foi corrigido e o fluxo
avançou um passo.

1. **Exception handlers sem `request`** (backend) — qualquer erro durante o
   fluxo OAuth derrubava com `TypeError` em vez de retornar um erro limpo.
2. **`global_config_service` não desembrulhava o envelope `{success, data,
   meta}`** ao buscar as credenciais OAuth globais do Google Calendar — os
   `client_id`/`client_secret`/`redirect_uri` vinham sempre `None`.
3. **Middleware de autenticação bloqueava o callback OAuth público** (`GET
   /api/v1/integrations/{provider}/callback`) com 401, pois o Google redireciona
   o navegador direto para essa URL sem header `Authorization`.
4. **Dependency do callback exigia token de usuário** e havia um `NameError`
   (`request` não existia como parâmetro) nos branches de erro do
   `oauth_callback`.
5. **Callback retornava JSON puro** em vez de redirecionar o navegador de
   volta para a aba Integrações do CRM.
6. **Conexão não era marcada como `connected`** na integração
   `google_calendar` — o frontend lia o status de `google_calendar`, mas só
   `google_calendar_credentials` era persistido.
7. **Frontend não tratava o redirect de volta** (`?google_calendar=success|error`)
   nem normalizava a resposta de `/agents/{id}/integrations` (formato
   `{configs: {...}}` vs array esperado).
8. **Lista de calendários (`/calendars`) vinha com envelope `{success, data,
   message}`**, mas o frontend lia `data.calendars` (sempre `undefined`) — o
   combobox "Selecione uma agenda" ficava vazio.
9. **`GET /sessions/{id}/messages` retornava 500** ("Object of type set is not
   JSON serializable") sempre que um evento de tool-call continha um `set` —
   quebrava o carregamento do histórico do chat após qualquer chamada de tool.
10. **Horários de atendimento configurados pelo usuário eram ignorados em
    TODO o módulo Google Calendar** — `business_hours.get("enabled")` nunca
    era verdadeiro porque o frontend salva `businessHours` só com flags por
    dia (sem chave `enabled` de nível superior). Resultado: `find_slots`
    sempre retornava `available_slots: []`, e o agente respondia que não havia
    nenhum horário disponível.
11. **`check_calendar_availability` falhava com "Unknown error"** sempre que
    o LLM enviava um datetime com timezone — o código concatenava `Z` em cima
    de um offset já existente (`...-03:00Z`), RFC3339 inválido para a API do
    Google. Além disso, **reagendar/cancelar criava eventos duplicados** (um
    novo evento "Cancelamento" + um novo evento, sem apagar o original), pois
    não existiam tools de update/delete.
12. **Lembretes de reunião ("avise 1h/5h/1dia/3dias antes") criavam 4 eventos
    extras no calendário** ("Lembrete: Reunião de Apresentação - X antes"),
    poluindo a agenda e ficando órfãos quando a reunião era reagendada/
    cancelada.

---

## Fix 1 — Exception handlers sem `request`

**Commit:** `ed09aab` — *fix: pass request to error_response in global exception
handlers*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/core/exception_handlers.py` (linhas 80 e 108)

`http_exception_handler` e `base_api_exception_handler` chamavam
`error_response()` sem o argumento obrigatório `request`, causando uma
exceção não tratada sempre que qualquer rota (inclusive as do fluxo OAuth)
caía em um desses handlers.

```python
# linhas ~77-83 e ~104-111
return error_response(
    request=request,   # <- adicionado
    code=error_code,
    message=error_message,
    details=error_details,
    ...
)
```

---

## Fix 2 — `global_config_service` não desembrulhava o envelope de resposta

**Commit:** `149e69f` — *fix: unwrap success_response envelope in
GlobalConfigService credential fetchers*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/services/global_config_service.py`
**Função:** `get_google_calendar_credentials` (linha 100) e equivalentes para
todos os outros providers (Google Sheets, GitHub, Notion, Stripe, Monday,
Atlassian, Asana, HubSpot, Linear, PayPal, Canva, Supabase — 13 ocorrências no
total).

A API da CRM (`/api/v1/integrations/{provider}/credentials`) responde com o
envelope padrão `{"success": true, "data": {...}, "meta": {...}}`. O código
fazia `data = response.json()` e tentava ler `data.get("google_calendar_client_id")`
diretamente — sempre `None`, pois essas chaves estão dentro de `data["data"]`.

```python
# linha 119 (e equivalentes para cada provider)
data = response.json().get("data", {})   # antes: data = response.json()
client_id = data.get("google_calendar_client_id")
client_secret = data.get("google_calendar_client_secret")
redirect_uri = data.get("google_calendar_redirect_uri")
```

**Impacto:** sem esse fix, `get_google_calendar_service_for_callback` (Fix 4)
sempre levantava `500 - Google Calendar OAuth credentials not configured`,
mesmo com o admin tendo configurado corretamente no painel global.

---

## Fix 3 — Middleware bloqueava o callback OAuth público

**Commit:** `a85ca3d` — *fix: allow public access to fixed OAuth callback
endpoints*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/middleware/evo_auth.py`

O `EvoAuthMiddleware` exigia um Bearer token em **toda** rota, incluindo
`GET /api/v1/integrations/{provider}/callback` — endpoint chamado diretamente
pelo navegador via redirect do Google, que nunca envia `Authorization`.

```python
# linha ~70, dentro de dispatch()
# Skip fixed OAuth callback endpoints (e.g. /api/v1/integrations/google-calendar/callback).
# These are hit directly by the browser via redirect from the OAuth provider
# (Google, GitHub, etc.) and never carry an Authorization header. They are
# protected instead by the signed/opaque `state` parameter.
if request.method == "GET" and self._is_oauth_callback_path(request.url.path):
    return await call_next(request)
```

```python
# linha 284 — novo método helper
def _is_oauth_callback_path(self, path: str) -> bool:
    """Check if path is a fixed OAuth callback endpoint (public by design)"""
    import re
    return bool(re.fullmatch(r"/api/v1/integrations/[^/]+/callback", path))
```

Segurança: a proteção desse endpoint passa a ser o parâmetro `state`
(assinado/opaco), não o Bearer token — padrão usual de callbacks OAuth.

---

## Fix 4 — Dependency exigia token de usuário + `NameError` no callback

**Commit:** `29d14b1` — *fix: don't require user token on the public Google
Calendar OAuth callback*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/api/google_calendar_routes.py`

Duas correções na mesma rota `oauth_callback`:

1. **Nova dependency** `get_google_calendar_service_for_callback` (linha 199),
   que não exige token de usuário (busca credenciais globais via
   `global_config_service` e usa apenas `db` para o restante do fluxo):

```python
# linha 199
async def get_google_calendar_service_for_callback(
    request: Request
) -> GoogleCalendarService:
    """Get Google Calendar service instance for the public OAuth callback (no user token required)."""
    from src.services.global_config_service import get_global_config_service

    config_service = get_global_config_service()
    credentials = await config_service.get_google_calendar_credentials()

    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    redirect_uri = credentials.get("redirect_uri")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google Calendar OAuth credentials not configured in global_config. ..."
        )

    core_service_url = os.getenv("CORE_SERVICE_URL", "http://localhost:5555/api/v1")
    auth_header = request.headers.get("Authorization", "")
    user_token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""

    return GoogleCalendarService(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        core_service_url=core_service_url,
        user_token=user_token
    )
```

2. **`oauth_callback` (linha 603)** passou a usar essa dependency e a receber
   `request: Request` como parâmetro (corrige o `NameError` — os branches de
   erro já referenciavam `request`, mas ele não existia na assinatura):

```python
async def oauth_callback(
    request: Request,             # <- adicionado (corrige NameError)
    code: str,
    state: str,
    service: GoogleCalendarService = Depends(get_google_calendar_service_for_callback),  # <- antes: get_google_calendar_service
    db: Session = Depends(get_db)
):
```

---

## Fix 5 — Callback agora redireciona para o frontend (em vez de JSON puro)

**Commit:** `9eeef75` — *feat: redirect Google Calendar OAuth callback back to
CRM frontend*
**Repo:** `evo-ai-processor-community`
**Arquivos:** `.env.example`, `src/api/google_calendar_routes.py`

Antes, `oauth_callback` retornava `success_response(...)` (JSON). O usuário
ficava vendo um JSON crú no navegador depois de autorizar no Google. Agora o
callback redireciona (`302`) de volta para
`{FRONTEND_URL}/agents/{agent_id}/edit?tab=integrations&google_calendar=success|error`.

**Nova variável de ambiente** (`.env.example`, linha ~35):
```
# Base URL of the CRM frontend, used to redirect the browser back after
# completing OAuth flows (e.g. Google Calendar)
FRONTEND_URL="http://localhost:5173"
```

**Helper novo** `_callback_redirect` (linha 576):
```python
def _callback_redirect(
    frontend_url: str,
    agent_id: Optional[str],
    result: str,
    message: Optional[str] = None
) -> RedirectResponse:
    """Build a redirect back to the CRM frontend with the OAuth result.

    Lands on the agent's Integrações tab when agent_id is known, otherwise
    falls back to the agents list.
    """
    if agent_id:
        url = f"{frontend_url}/agents/{agent_id}/edit?tab=integrations&google_calendar={result}"
    else:
        url = f"{frontend_url}/agents/list?google_calendar={result}"
    if message:
        url += f"&message={quote(message)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
```

`oauth_callback` (linha 603) agora usa `_callback_redirect(...)` em todos os
caminhos (sucesso, `state` inválido, `complete_authorization` falhou,
`ValueError`, `Exception` genérica), sempre com `frontend_url =
os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")` (linha 621).

---

## Fix 6 — Conexão não era marcada como `connected` (`google_calendar`)

**Commit:** `50c1b24` — *fix: mark google_calendar integration as connected
after OAuth completes*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/services/google_calendar_service.py`

`complete_authorization` (linha 121) salvava as credenciais em
`google_calendar_credentials`, mas o frontend lê o status de conexão (`ATIVO`
/ `CONFIGURAR`) a partir da integração `google_calendar`. Sem esse fix, a aba
Integrações continuava mostrando a tela "Conectar com Google" mesmo após uma
autorização bem-sucedida.

```python
# dentro de complete_authorization, linha ~208
calendars = await self.get_calendars(agent_id, db=db)

# Mark the user-facing "google_calendar" integration as connected.
# Credentials are stored under "google_calendar_credentials", but the
# frontend reads connection status from "google_calendar" — without
# this it keeps showing the "Connect with Google" screen even after
# a successful authorization.
await self._mark_connected(agent_id, email, calendars, db=db)
```

Novo método `_mark_connected` (linha 567), que faz upsert preservando
configurações já existentes do usuário (calendário selecionado, regras de
agendamento etc.) e marca `connected: True`:

```python
async def _mark_connected(
    self,
    agent_id: str,
    email: Optional[str],
    calendars: List[Dict[str, Any]],
    db: Optional[Any] = None
) -> None:
    """Update the "google_calendar" integration with the connection status.

    Preserves any existing user-configured settings (e.g. selected calendar,
    booking rules) while marking the integration as connected and refreshing
    the email/calendars returned by Google.
    """
    try:
        if db:
            from src.services.agent_service import (
                get_agent_integration_by_provider,
                upsert_agent_integration,
            )
            existing_config = await get_agent_integration_by_provider(
                db, agent_id, "google_calendar"
            ) or {}
            existing_config.update({
                "provider": "google_calendar",
                "connected": True,
                "email": email,
                "calendars": calendars,
            })
            await upsert_agent_integration(db, agent_id, "google_calendar", existing_config)
        else:
            existing_config = await self.get_configuration(agent_id) or {}
            existing_config.update({
                "provider": "google_calendar",
                "connected": True,
                "email": email,
                "calendars": calendars,
            })
            await self.save_configuration(agent_id, existing_config)
    except Exception as e:
        logger.error(f"Error marking Google Calendar integration as connected: {e}")
```

---

## Fix 7 — Frontend: redirect handling + normalização de `/agents/{id}/integrations`

**Commits:**
- `ab01ccb` — *feat: handle Google Calendar OAuth redirect and add fork CI for
  ghcr.io/agbid*
- `8dcf5c1` — *fix: normalize agent-integrations response shape (object vs
  array)*

**Repo:** `evo-ai-frontend-community`

### 7.1 — Toast + reload ao voltar do OAuth

**Arquivo:** `src/pages/Customer/Agents/Agent/sections/IntegrationsSection.tsx`
(linhas ~53-69)

Novo `useEffect` que lê `?google_calendar=success|error&message=...` (vindo do
Fix 5), mostra um toast e dispara `reloadConfigs()`; depois limpa os query
params:

```tsx
// Handle the redirect back from the Google Calendar OAuth callback
// (?google_calendar=success|error&message=...), shown as a toast.
useEffect(() => {
  const googleCalendarResult = searchParams.get('google_calendar');
  if (!googleCalendarResult) return;

  const message = searchParams.get('message');

  if (googleCalendarResult === 'success') {
    toast.success(t('edit.integrations.googleCalendar.connected') || 'Google Calendar conectado com sucesso');
    reloadConfigs();
  } else if (googleCalendarResult === 'error') {
    toast.error(
      message || t('edit.integrations.googleCalendar.connectError') || 'Falha ao conectar com o Google Calendar'
    );
  }

  searchParams.delete('google_calendar');
  searchParams.delete('message');
  setSearchParams(searchParams, { replace: true });
  // eslint-disable-next-line react-hooks/exhaustive-deps
}, []);
```

Novas chaves de i18n (`edit.integrations.googleCalendar.connected` /
`connectError`) adicionadas em `en`, `es`, `fr`, `it`, `pt`, `pt-BR`
(`src/i18n/locales/*/aiAgents.json`).

### 7.2 — Google Calendar/Sheets sempre disponíveis na lista de integrações

**Arquivo:** mesmo arquivo, linha 131 (`ALWAYS_AVAILABLE_INTEGRATIONS`):

```tsx
// Integrações que sempre estão disponíveis porque o usuário fornece sua
// própria credencial (API key) — não dependem de OAuth global configurado
// pelo administrador. Google Calendar / Sheets usam OAuth por agente
// (autorização individual via "Conectar com Google"), então também ficam
// sempre disponíveis — não há um passo de configuração de admin que
// popule `credentialsConfigured` antes do primeiro uso.
const ALWAYS_AVAILABLE_INTEGRATIONS = [
  'elevenlabs',
  'knowledge-nexus',
  'google-calendar',
  'google-sheets',
];
```

### 7.3 — `googleCalendarService` desembrulha `data.data`

**Arquivo:** `src/services/integrations/googleCalendarService.ts`

```tsx
// generateAuthorization (linha 14) e completeAuthorization (linha 30)
return data.data;   // antes: return data;
```

### 7.4 — `normalizeIntegrationConfigs` (formato `{configs: {...}}` → array)

**Arquivo:** `src/utils/apiHelpers.ts` (linha 199, interface na linha 186)

`GET /agents/{agent_id}/integrations` retorna
`{ configs: { [provider]: config }, credentials_configured: {...} }`, com a
flag `connected` dentro de cada `config` — não o array `[{provider, config}]`
que os consumidores esperavam.

```tsx
export interface IntegrationConfigItem {
  provider: string;
  config: Record<string, unknown>;
}

/**
 * Normalize the response of GET /agents/{agent_id}/integrations into a flat
 * array of { provider, config } items.
 *
 * The endpoint returns `{ configs: { [provider]: config }, credentials_configured: {...} }`,
 * with a `connected` flag set on each config — not the `[{ provider, config }]`
 * array shape consumers historically expected.
 */
export function normalizeIntegrationConfigs(data: unknown): IntegrationConfigItem[] {
  if (Array.isArray(data)) return data as IntegrationConfigItem[];

  const configs = (data as { configs?: Record<string, Record<string, unknown>> } | null | undefined)
    ?.configs;
  if (!configs) return [];

  return Object.entries(configs)
    .filter(([, config]) => config?.connected === true)
    .map(([provider, config]) => ({ provider, config }));
}
```

Usado em:
- `src/services/agents/agentIntegrationsService.ts` linha 34 —
  `getAgentIntegrations`
- `src/services/agents/agentService.ts` linha 84 — `getAgentIntegrations`

---

## Fix 8 — Lista de calendários (`/calendars`) com envelope não desembrulhado

**Commit:** `b698022` — *fix(google-calendar): unwrap response envelope when
fetching calendar list*
**Repo:** `evo-ai-frontend-community`
**Arquivo:** `src/services/integrations/googleCalendarService.ts` (linha 53,
função `getCalendars`)

O backend responde `{success: true, data: [...], message: "..."}`, mas o
código lia `data.calendars` (sempre `undefined`) — o combobox "Selecione uma
agenda" no dialog de configuração ficava vazio mesmo com a conta conectada e
com calendários disponíveis.

```tsx
async getCalendars(agentId: string): Promise<GoogleCalendarItem[]> {
  try {
    const response = await api.get(
      `/agents/${agentId}/integrations/google-calendar/calendars`
    );
    const data = extractData<GoogleCalendarItem[]>(response);
    return Array.isArray(data) ? data : [];
  } catch (error) {
    console.error('GoogleCalendarService.getCalendars error:', error);
    throw error;
  }
}
```

**Verificado via Playwright em produção**: combobox passou a listar os 4
calendários da conta `mcc@agencia.bid` ("Feriados no Brasil", "Projefarma",
"AGÊNCIA BID (Principal)", "Família").

---

## Fix 9 — `GET /sessions/{id}/messages` 500 (`set` não serializável)

**Commit:** `40f7215` — *fix(google-calendar): respect saved business hours and
serialize set values in chat history*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/api/session_routes.py`, helper `process_dict` (linha 632)

Sempre que um evento de tool-call (ex.: `check_calendar_availability`)
guardava algum valor do tipo `set`/`frozenset` no histórico, `process_dict`
não sabia serializar isso e `GET /api/v1/sessions/{id}/messages` retornava 500
(`TypeError: Object of type set is not JSON serializable`) repetidamente —
quebrava o carregamento/atualização do histórico do chat após qualquer
chamada de tool.

```python
# dentro de process_dict(d), linha ~642 (caso dict)
elif isinstance(value, (set, frozenset)):
    d[key] = list(value)
elif isinstance(value, dict):
    process_dict(value)
elif isinstance(value, list):
    for item in value:
        if isinstance(item, (dict, list)):
            process_dict(item)

# caso list, linha ~660
elif isinstance(item, (set, frozenset)):
    d[i] = list(item)
elif isinstance(item, (dict, list)):
    process_dict(item)
```

**Verificado via Playwright em produção**: `GET
/api/v1/sessions/7f81a62f-5cf9-4212-8cfd-dda2283af65b/messages` passou de `500`
para `200`.

---

## Fix 10 — Horários de atendimento ignorados em todo o módulo (causa raiz do "não retorna agenda")

**Commit:** `40f7215` (mesmo commit do Fix 9) — *fix(google-calendar): respect
saved business hours and serialize set values in chat history*
**Repo:** `evo-ai-processor-community`

### Causa raiz

O frontend salva `settings.businessHours` como um mapa por dia, **sem** chave
`enabled` de nível superior:

```json
"businessHours": {
  "monday":    { "enabled": true,  "start": "08:00", "end": "18:00" },
  "tuesday":   { "enabled": true,  "start": "08:00", "end": "18:00" },
  "wednesday": { "enabled": true,  "start": "08:00", "end": "18:00" },
  "thursday":  { "enabled": true,  "start": "08:00", "end": "18:00" },
  "friday":    { "enabled": true,  "start": "08:00", "end": "18:00" },
  "saturday":  { "enabled": false, "start": "08:00", "end": "18:00" },
  "sunday":    { "enabled": false, "start": "08:00", "end": "18:00" }
}
```

(Estrutura confirmada na config real do agente
`da2892b5-0b1c-4be5-a713-5422749a34bb`, tabela `evo_core_agents`, coluna
`config -> integrations -> 'google-calendar' -> settings -> businessHours`.)

Porém **4 lugares diferentes** no backend checavam
`business_hours.get("enabled")` (chave que nunca existe) antes de olhar os
dias — então a condição era **sempre falsa**, e os horários configurados pelo
usuário eram **sempre ignorados**:

| Arquivo | Linha (antes) | Efeito do bug |
|---|---|---|
| `src/services/adk/tools/google_calendar/base.py` | 112 (`is_within_business_hours`) | `return True` sempre (nenhuma restrição é aplicada ao checar um slot único) |
| `src/services/adk/tools/google_calendar/check_availability.py` | 303 (`_find_available_slots`) | **todos os dias eram pulados** → `find_slots=True` sempre retornava `available_slots: []` |
| `src/services/adk/tools/google_calendar/check_availability.py` | 415 (docstring da tool) | a tool nunca informava ao LLM quais são os horários configurados |
| `src/services/adk/tools/google_calendar/create_event.py` | 277 (docstring da tool) | idem, para `create_event` |
| `src/services/adk/agents/llm_agent_builder.py` | 876 (prompt do agente) | instruções de "Scheduling is restricted to business hours: ..." nunca eram incluídas no prompt |

O item mais visível era o de `check_availability.py:303`: como
`business_hours.get("enabled")` é sempre `None`/falsy, o branch original
(`day_config = {}` para todo dia → `if not day_config or not
day_config.get("enabled"): continue`) **pulava todos os dias do range**,
fazendo `_find_available_slots` retornar sempre `"Found 0 available time
slots"` — exatamente o sintoma relatado: *"quando peço horários, ele não
retorna nenhuma agenda, mesmo chamando as tools"*.

### Correção aplicada

Trocar `business_hours.get("enabled")` por uma checagem de **truthiness do
dict** (`if business_hours:` / `if not business_hours:`) — ou seja, "existe
configuração de horários salva?" — e deixar os flags **por dia**
(`businessHours.<dia>.enabled`) decidirem cada dia individualmente. Isso é
consistente com o "sem restrição" já usado como default quando
`businessHours` está totalmente vazio (agente nunca configurou a aba
"Horários").

#### `base.py` — `is_within_business_hours` (linha 112)

```python
if not business_hours:
    return True
```
(antes: `if not business_hours or not business_hours.get("enabled"):`)

#### `check_availability.py` — `_find_available_slots` (linhas 287, 303-324)

```python
logger.info(f"Finding available slots: business_hours_configured={bool(business_hours)}, timezone={timezone}, min_advance={min_advance_time}h, max_duration={max_duration}min")
...
while current_day <= end_day:
    if business_hours:
        # Get business hours for this day
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        day_name = day_names[current_day.weekday()]
        day_config = business_hours.get(day_name, {})

        logger.debug(f"Checking {day_name} ({current_day.date()}): enabled={day_config.get('enabled') if day_config else False}")

        if not day_config or not day_config.get("enabled"):
            # Skip non-business days
            logger.debug(f"Skipping {day_name} - not a business day")
            current_day += timedelta(days=1)
            continue

        # Parse business hours for this day
        start_time_str = day_config.get("start", "09:00")
        end_time_str = day_config.get("end", "18:00")
    else:
        # No business hours configured at all: don't restrict the search window,
        # consistent with is_within_business_hours()'s "no restriction" default.
        start_time_str = "00:00"
        end_time_str = "23:59"
```

#### `check_availability.py` — docstring da tool (linha 415)

```python
bh_description = ""
if business_hours:
    bh_description = "\n\nBUSINESS HOURS CONFIGURED:\n"
    ...
```
(antes: `if business_hours and business_hours.get("enabled"):`)

#### `create_event.py` — docstring da tool (linha 277)

```python
bh_description = ""
if business_hours:
    bh_description = "\n\nBUSINESS HOURS CONFIGURED:\n"
    ...
```
(mesma mudança que em `check_availability.py`)

#### `llm_agent_builder.py` — instruções de prompt (linha 876)

```python
business_hours = calendar_settings.get("businessHours", {})
if business_hours:
    enabled_days = []
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
        day_config = business_hours.get(day, {})
        if day_config.get("enabled"):
            ...
```
(antes: `if business_hours and business_hours.get("enabled"):`)

### Validação em produção

Configuração real do agente (aba "Horários", já salva pelo usuário):
Segunda a Sexta 08:00–18:00, Sábado/Domingo desativados.

Teste via "Teste seu agente" → pergunta: *"Quero agendar uma reunião de
apresentação. Quais horários você tem disponíveis nos próximos dias?"*

Resultado (sessão `553c2758-1da4-437b-a2c4-645336c1c09c`): o agente chamou
`check_calendar_availability` e retornou slots de 30 em 30 min entre 08:00 e
18:00 para 12/06 (sexta), 15/06 (segunda) e 16/06 (terça) — **pulando o final
de semana (13-14/06)**, exatamente conforme configurado.

---

## Fix 11 — RFC3339 inválido em `check_availability`, tools de `update`/`delete` e "Sempre aberto"

**Commit:** `e35850f` — *fix(google-calendar): fix invalid RFC3339 timestamps and
add update/delete event tools*
**Repo:** `evo-ai-processor-community`
**Arquivos:** `src/services/adk/tools/google_calendar/base.py`,
`check_availability.py`, novos `update_event.py` e `delete_event.py`,
`tool_builder.py`, `__init__.py`, `llm_agent_builder.py`

### Problema 1 — `check_calendar_availability` retornava "Unknown error"

`check_availability` montava `timeMin`/`timeMax` concatenando `+ 'Z'` em cima
de um `datetime` que **já** tinha timezone (ex.: `2026-06-17T10:00:00-03:00`),
gerando `2026-06-17T10:00:00-03:00Z` — timestamp RFC3339 inválido, rejeitado
pela API do Google com `400`. Sempre que o LLM enviava um ISO timestamp com
offset, a tool quebrava.

```python
# base.py, dentro de check_availability()
# Build RFC3339 timestamps for the Google Calendar API.
# Timezone-aware datetimes already include an offset (e.g. "-03:00"),
# so appending 'Z' would produce an invalid timestamp like
# "...-03:00Z" and the API would reject the request.
time_min = start_time.isoformat() if start_time.tzinfo else start_time.isoformat() + 'Z'
time_max = end_time.isoformat() if end_time.tzinfo else end_time.isoformat() + 'Z'
```

### Problema 2 — Reagendar/cancelar criava eventos duplicados de "Cancelamento"

Sem uma tool para editar/apagar um evento existente, o agente reagendava
criando um evento "Cancelamento da reunião" **+** um novo evento, deixando o
original intacto na agenda — duplicando entradas a cada reagendamento.

**Fix:** duas novas tools, registradas em `tool_builder.py` e
`tools/google_calendar/__init__.py`:

- **`update_calendar_event`** (`update_event.py`) — reagenda/edita um evento
  existente por `event_id` (só altera os campos informados; os demais
  permanecem).
- **`delete_calendar_event`** (`delete_event.py`) — cancela/apaga um evento
  existente por `event_id` (`notify_attendees` opcional).

`check_calendar_availability` agora retorna o `id` de cada item em
`conflicting_events`, para o agente localizar o evento a reagendar/cancelar:

```python
# check_availability.py, dentro de "conflicting_events"
"id": event.get("id"),
"summary": event.get("summary", "Untitled"),
"start": event.get("start", {}).get("dateTime"),
"end": event.get("end", {}).get("dateTime")
```

Instruções do agente (`llm_agent_builder.py`) atualizadas para deixar
explícito o fluxo correto:

```python
"create_calendar_event to schedule meetings, update_calendar_event to "
"reschedule or edit an existing meeting, and delete_calendar_event to "
"cancel one. To cancel or reschedule a meeting, first find its 'id' via "
"check_calendar_availability's conflicting_events, then call "
"update_calendar_event or delete_calendar_event with that id - never "
"create a new 'cancellation' event. All restrictions are enforced "
"automatically by the tools."
```

### Extra — suporte a "Sempre aberto" (`alwaysOpen`)

`is_within_business_hours` e `_find_available_slots` passaram a receber
`always_open` (lido de `settings.alwaysOpen`); quando `true`, os horários de
atendimento são totalmente ignorados (equivalente a não ter `businessHours`
configurado).

### Validação em produção

Testado um reagendamento real via chat: o agente chamou `delete_calendar_event`
para o evento original de 15/06 14h (`63bacss4g91mol8hg8vvptd8a4`, status
`success`) e em seguida `create_calendar_event` para 17/06 10h-11h — sem criar
nenhum evento "Cancelamento" duplicado. (O agente optou por delete+create em
vez de `update_calendar_event` — escolha do LLM, não limitação da tool; ambas
funcionam corretamente.)

---

## Fix 12 — Lembretes nativos do evento via `reminder_minutes_before` (sem criar eventos "Lembrete")

**Commit:** `3ce94a1` — *feat(google-calendar): support native event reminders
via create_calendar_event*
**Repo:** `evo-ai-processor-community`
**Arquivos:** `src/services/adk/tools/google_calendar/base.py`,
`create_event.py`, `llm_agent_builder.py`

### Problema

Quando configurado para avisar o cliente "1 hora, 5 horas, 1 dia e 3 dias
antes" da reunião, o agente criava **4 eventos extras** no calendário
("Lembrete: Reunião de Apresentação - X antes"). Isso poluía a agenda e, pior,
ao reagendar a reunião (via delete+create, ver Fix 11) esses 4 eventos
ficavam **órfãos** — continuavam apontando para a data antiga.

### Fix

Novo parâmetro opcional `reminder_minutes_before: List[int]` em
`create_calendar_event`, que popula `event["reminders"]` na chamada
`events().insert()` em vez de criar eventos separados:

```python
# base.py, dentro de create_event(), antes de "# Create event"
# Add custom reminders instead of relying on the calendar's defaults.
# Google Calendar allows at most 5 overrides, each between 0 and
# 40320 minutes (4 weeks) before the event.
if reminder_minutes_before:
    overrides = [
        {'method': 'popup', 'minutes': minutes}
        for minutes in reminder_minutes_before
        if 0 <= minutes <= 40320
    ][:5]
    if overrides:
        event['reminders'] = {
            'useDefault': False,
            'overrides': overrides,
        }
```

`create_event.py` repassa o parâmetro e documenta no docstring dinâmico da
tool (com exemplo `reminder_minutes_before=[60, 300, 1440, 4320]` para
1h/5h/1dia/3dias). Instruções do agente (`llm_agent_builder.py`) reforçam:

```python
"If you need to set alert/reminder notifications before a meeting "
"(e.g. 1 hour, 5 hours, 1 day, 3 days before), pass the "
"'reminder_minutes_before' argument to create_calendar_event (a list "
"of minutes before the start, e.g. [60, 300, 1440, 4320]). This sets "
"notifications on the meeting itself - never create separate "
"'Lembrete'/'Reminder' calendar events for this purpose. Rescheduling "
"the meeting with update_calendar_event automatically moves its "
"reminders too, so no extra events need to be managed."
```

Como os lembretes agora são parte do próprio evento, `update_calendar_event`
move-os automaticamente ao reagendar, e `delete_calendar_event` os remove
junto ao cancelar — nada fica órfão.

**Prompt "Comportamento" do agente `sdr`** (`da2892b5-0b1c-4be5-a713-5422749a34bb`,
editado via UI) também foi atualizado para pedir os 4 lembretes via
`reminder_minutes_before` em vez de "4 avisos" como eventos separados.

### Validação em produção

Agendamento real criado pelo agente (evento `6a88enu1le875lm0t235v1lrqs`,
12/06/2026 09:00-10:00):

```json
"reminders": {
    "useDefault": false,
    "overrides": [
        {"method": "popup", "minutes": 1440},
        {"method": "popup", "minutes": 300},
        {"method": "popup", "minutes": 60},
        {"method": "popup", "minutes": 4320}
    ]
}
```

Confirmado visualmente no Google Agenda (`mcc@agencia.bid`, via Playwright):
o card do evento mostra "🔔 1 hora antes / 5 horas antes / 1 dia antes / 3
dias antes" — e **nenhum evento "Lembrete" separado foi criado**.

### Limitação conhecida (comportamento do Google, não um bug)

`reminders.overrides` pertence à **cópia do evento na agenda do organizador**
(`mcc@agencia.bid`). O Google Calendar **não propaga** lembretes customizados
para a cópia do evento na agenda dos convidados — cada convidado usa as
próprias preferências de notificação (mesmo após aceitar o convite). Não há
endpoint da API que force lembretes na agenda de terceiros.

Se for necessário avisar o **cliente** (não só a agência) antes da reunião,
será preciso uma automação separada — ex.: ao criar o evento, agendar um
`ScheduledAction` (`send_whatsapp`) no `evo-ai-crm-community`, que já tem a
infraestrutura de disparo via `ScheduledActionsProcessorJob`
(Sidekiq, roda a cada 1 min) + `ExecutorService` (já integrado com WhatsApp
via Evolution API). Não implementado neste ciclo — ver "Pendências
conhecidas".

---

## Fixes adicionais (sessão 12/06/2026) — fluxo de testes e agendamento ponta a ponta

Depois do Fix 12, foi feita uma rodada de testes ponta a ponta do agente
`inter_rural_atendimento` (`afbeaa63-eeb3-418e-901d-5eacc32fe75c`) usando
"Teste seu agente" com um contato real do CRM. Isso exigiu mais 3 correções
(uma de infraestrutura de teste, uma de uma rota da CRM, e uma na instrução do
agente) para o fluxo de agendamento funcionar de forma confiável.

---

## Fix 13 — Lembretes não devem ser instrução global

**Commit:** `38a48e1` — *fix(google-calendar): stop injecting
reminder_minutes_before guidance for all agents*
**Repo:** `evo-ai-processor-community`
**Arquivo:** `src/services/adk/agents/llm_agent_builder.py` (-12 linhas)

A instrução global injetada em **todo** agente com Google Calendar conectado
mandava usar `reminder_minutes_before` (Fix 12). Mas esses lembretes só
notificam a agenda do **organizador** (`mcc@agencia.bid`), não o
cliente/convidado — não faz sentido como default para todos os agentes.
Removida a instrução global; o docstring da própria tool
`create_calendar_event` já documenta o parâmetro, e cada agente que quiser
usá-lo pede isso no seu próprio prompt "Comportamento" (foi o que foi feito no
Fix 16, abaixo, para o `inter_rural_atendimento`).

---

## Fix 14 — `contact_id` no chat de teste ("Teste seu agente") para injetar contexto real da CRM

**Commits:**
- `5b354f9` (`evo-ai-processor-community`) — *feat(chat): support contact_id
  in test chat for CRM context injection*
- `54e3a29` (`evo-ai-frontend-community`) — *feat(agents): add contact
  selector to agent test chat*

### Problema

Tools como `update_contact` e `transfer_to_human` dependem de
`evoai_crm_data.contact_id`/`conversation_id` no session state — dados que só
existem numa conversa real do WhatsApp. No "Teste seu agente" (sem CRM por
trás), essas tools não tinham contato/conversa para operar, então **ou
falhavam, ou (pior) o LLM simulava sucesso sem chamar a tool de verdade**
("vou te transferir..." sem `transfer_to_human`).

### Fix

**Backend** (`evo-ai-processor-community`):
- `src/schemas/chat.py` — novo campo opcional `ChatRequest.contact_id: Optional[str]`.
- `src/api/chat_routes.py` — `build_contact_test_metadata(contact_id)`: busca
  `/contacts/{contact_id}` e `/contacts/{contact_id}/conversations` na CRM via
  `EvoCrmClient`, e monta:
  ```python
  {
    "contact": {...},
    "contactId": "...",
    "evoai_crm_data": {
      "contact_id": "...",
      "contact": {...},
      "conversation_id": "..."  # se existir alguma conversa
    }
  }
  ```
  Quando `payload.contact_id` é informado, esse `metadata` é passado para
  `run_agent_adk(...)`, igual a uma conversa real via webhook.
- `src/api/a2a_routes.py` — fallback: se `evoai_crm_data.conversation_id` não
  vier preenchido, usa o `context_id` da chamada A2A.

**Frontend** (`evo-ai-frontend-community`):
- Novo componente `AgentChatContactSelector.tsx` — busca contatos
  (`contactsService.searchContacts`, debounce 300ms) e mostra um dropdown ao
  lado do nome do agente no "Teste seu agente".
- `AgentChatContext.tsx` — novo estado `selectedContact`; ao enviar mensagem,
  passa `contact_id: selectedContact?.id` em `sendChatMessage(...)`.
- `chatService.ts` / `types/agents/agent.ts` — `ChatRequest.contact_id?: string`.
- i18n: `chat.testContactPlaceholder`, `chat.noContactsFound` em todos os
  idiomas (`en`, `es`, `fr`, `it`, `pt`, `pt-BR`).

### Validação em produção

Com o contato "Fabricio Sahdo" selecionado no teste:
- `update_contact` alterou de fato o e-mail do contato no CRM.
- `transfer_to_human` reatribuiu de fato a conversa
  (`34b6991a-4335-456d-9bf0-12d5c3e969a9`) para o agente humano
  `mcc@agencia.bid` na CRM em produção.

> ⚠️ Esses são efeitos colaterais **reais** em dados de produção (não
> simulados) — é o comportamento esperado/desejado do recurso, mas qualquer
> teste futuro com contato selecionado também vai gerar mudanças reais.

---

## Fix 15 — `GET /api/v1/contacts/:id/conversations` retornava `204 No Content`

**Commit:** `2c217d7` (`evo-ai-crm-community`, PR
[#144](https://github.com/evolution-foundation/evo-ai-crm-community/pull/144),
aberto/não mergeado — hotfix já aplicado em produção via `docker cp` + restart
do Puma)
**Arquivo:** `app/controllers/api/v1/contacts/conversations_controller.rb`

### Problema

Descoberto durante o Fix 14: `build_contact_test_metadata` chamava
`GET /contacts/{id}/conversations` (com `X-Service-Token`) e recebia `HTTP
204` com corpo vazio — a action `index` populava `@conversations` mas **não
tinha view nem `render`**, então o Rails respondia `204` por padrão (nenhuma
conversa era encontrada para preencher `evoai_crm_data.conversation_id`).

### Fix

```ruby
@conversations = conversations.order(last_activity_at: :desc).limit(20)

success_response(data: ConversationSerializer.serialize_collection(@conversations, include_labels: true))
```

### Validação em produção

`curl` (de dentro do container do processor, com `X-Service-Token`) passou de
`204` vazio para `200` com
`{"success":true,"data":[{"id":"34b6991a-...","inbox_id":...,"status":...,...}]}`.

---

## Fix 16 — Agente `inter_rural_atendimento` inventava horários alternativos fora do expediente

**Tipo:** alteração de instrução do agente (coluna `instruction` em
`evo_core_agents`, **não** é um commit de código) — aplicada via `rails
runner` em produção, mesmo mecanismo usado para o `{_system_data}`.
**Agente:** `inter_rural_atendimento`
(`afbeaa63-eeb3-418e-901d-5eacc32fe75c`)

### Problema

`create_calendar_event` retornava ❌ "Event time is outside business hours"
mesmo depois de `check_calendar_availability` retornar ✅. Investigação (com
`find_slots=true`, dump do `businessHours` real do agente — seg-sex 08-18h,
sáb/dom desativado — e reprodução via Playwright) mostrou que **o código
estava correto**:

- `check_calendar_availability` retornava corretamente `available: false` +
  motivo, com `status: "success"` (por isso o ✅ na UI mesmo indisponível).
- O agente avisava certo que o horário pedido (ex.: "amanhã às 10h", quando
  "amanhã" cai num sábado) estava fora do expediente...
- ...mas **inventava** horários alternativos ("amanhã às 11h ou segunda às
  9h") sem chamar a tool — e quando o cliente aceitava, `create_calendar_event`
  rejeitava corretamente (`❌`), porque o horário inventado também caía fora
  do expediente.

Ou seja: **`is_within_business_hours`, `check_calendar_availability` e
`create_calendar_event` são consistentes** — o bug era 100% de *prompt*
(hallucination de horários), não de código.

### Fix

Parágrafo "SEU OBJETIVO É AGENDAR REUNIÕES" da instrução do agente alterado
para exigir `find_slots=true` antes de sugerir qualquer horário:

```
SEU OBJETIVO É AGENDAR REUNIÕES, não esperar o cliente decidir. Assim que
perceber interesse, não pergunte "qual dia e horário você prefere?". Em vez
disso, chame check_calendar_availability com find_slots=true para os próximos
dias e sugira 2 horários REAIS retornados pela ferramenta (ex: "consigo te
encaixar terça às 10h ou quinta de manhã, qual fica melhor pra você?"). NUNCA
invente ou estime horários por conta própria - use sempre os horários que
vierem da ferramenta. Conduza o cliente pra escolher um desses horários. Se
ele pedir outro dia ou um horário específico, chame check_calendar_availability
de novo (com find_slots=true) antes de responder, e ofereça novas opções do
mesmo jeito, sempre com horários reais da ferramenta.
```

### Validação em produção (sessão `5843444a`)

1. "Reunião pra amanhã às 10h" (hoje = sexta 12/06, "amanhã" = sábado 13/06,
   desativado) → `check_calendar_availability` ❌ disponível → agente respondeu
   honestamente, sem inventar horário, oferecendo buscar alternativas reais.
2. "Pode ver sim" → `check_calendar_availability(find_slots=true,
   start_date=2026-06-15, end_date=2026-06-19)` → 92 slots reais retornados.
3. Agente sugeriu "terça às 10h ou quarta às 14h" — ambos presentes
   literalmente em `available_slots` (`2026-06-16T10:00:00`,
   `2026-06-17T14:00:00`).

---

## Estrutura de dados de referência

### `agent.config.integrations["google-calendar"]` (no Postgres, tabela `evo_core_agents`, coluna `config`)

```json
{
  "google-calendar": {
    "provider": "google_calendar",
    "connected": true,
    "email": "mcc@agencia.bid",
    "calendars": [
      { "id": "...", "name": "Feriados no Brasil", "primary": false, "selected": false },
      { "id": "...", "name": "Projefarma", "primary": false, "selected": false },
      { "id": "mcc@agencia.bid", "name": "AGÊNCIA BID", "primary": true, "selected": true },
      { "id": "...", "name": "Família", "primary": false, "selected": false }
    ],
    "settings": {
      "selectedCalendarId": "mcc@agencia.bid",
      "alwaysOpen": false,
      "minAdvanceTime": { "enabled": true, "value": 1, "unit": "hours" },
      "maxDistance": { "enabled": true, "value": 3, "unit": "days" },
      "maxDuration": { "value": 1, "unit": "hours" },
      "simultaneousBookings": { "enabled": false, "limit": 1 },
      "businessHours": {
        "monday":    { "enabled": true,  "start": "08:00", "end": "18:00" },
        "tuesday":   { "enabled": true,  "start": "08:00", "end": "18:00" },
        "wednesday": { "enabled": true,  "start": "08:00", "end": "18:00" },
        "thursday":  { "enabled": true,  "start": "08:00", "end": "18:00" },
        "friday":    { "enabled": true,  "start": "08:00", "end": "18:00" },
        "saturday":  { "enabled": false, "start": "08:00", "end": "18:00" },
        "sunday":    { "enabled": false, "start": "08:00", "end": "18:00" }
      },
      "meetIntegration": true,
      "allowAvailabilityCheck": true,
      "restrictedHours": { "enabled": false, "allowedTimes": ["09:00"] },
      "distributionMode": "sequential",
      "bookingFields": [ ... ]
    }
  }
}
```

> Observação: a chave usada na config é `"google-calendar"` (com hífen), mas o
> `provider` interno é `"google_calendar"` (com underscore) — os dois nomes
> coexistem dependendo da camada (rota REST vs. persistência interna).

---

## Pendências conhecidas (fora do escopo desta correção)

- **Lembretes para o cliente (não só para a agência)**: `reminder_minutes_before`
  (Fix 12) só configura notificações na agenda do organizador
  (`mcc@agencia.bid`). Avisar o cliente por WhatsApp/e-mail antes da reunião
  exigiria uma automação nova (ex.: `ScheduledAction` no
  `evo-ai-crm-community`, reaproveitando `ScheduledActionsProcessorJob` +
  `ExecutorService`) — ver Fix 12 para o esboço da solução.
- **Erro de conexão intermitente com a OpenAI** (`litellm.APIError:
  OpenAIException - Connection error` /
  `httpcore.ConnectError: [Errno -5] No address associated with hostname`):
  diagnosticado como falha transitória de DNS dentro do container do
  processor (resolver embutido do Docker Swarm sob carga de conexões
  concorrentes ao inicializar várias tools). Confirmado como transitório
  (retry funcionou). Nenhuma alteração de código foi feita para isso — se
  voltar a ocorrer com frequência, considerar configurar `num_retries`/`timeout`
  no `LiteLlm(...)` em `src/services/adk/agents/llm_agent_builder.py` (linha
  ~1121) ou investigar o resolver DNS do Swarm.
- **Erro de CORS em `GET /agents/{id}/integrations`** observado durante os
  testes em produção (`Access-Control-Allow-Origin` ausente) — não bloqueia o
  fluxo principal (a página segue funcionando via outros endpoints), mas pode
  causar mensagens de erro no console / falha ao recarregar a lista de
  integrações em algumas condições. Não investigado neste ciclo.
- **Reverter alteração temporária de CORS**: remover `http://localhost:5173`
  de `CORS_ORIGINS` em `evocrm_evocrm_auth` e `evocrm_evocrm_crm` (usado
  durante debugging anterior).
- **Rotacionar a senha root SSH** do servidor `213.199.37.111` (usada nas
  sessões de deploy).
- **Revisar/revogar token OAuth de teste antigo** (`ya29...`) de uma sessão
  anterior — **não** o token de `mcc@agencia.bid`, que deve permanecer ativo.
- **Mergear PRs já hotfixados em produção** em `evo-ai-crm-community`:
  [#143](https://github.com/evolution-foundation/evo-ai-crm-community/pull/143)
  (PermissionFilterService) e
  [#144](https://github.com/evolution-foundation/evo-ai-crm-community/pull/144)
  (Fix 15, `/contacts/:id/conversations`).
- **Efeito colateral real do Fix 14**: o teste de `transfer_to_human` reatribuiu
  de fato a conversa `34b6991a-4335-456d-9bf0-12d5c3e969a9` (contato "Fabricio
  Sahdo") para o agente humano `mcc@agencia.bid` em produção. Avaliar se
  precisa ser revertido manualmente.

---

## Resumo dos commits

### `evo-ai-processor-community` (branch `production`, fork `agbid`)

| Commit | Mensagem |
|---|---|
| `ed09aab` | fix: pass request to error_response in global exception handlers |
| `149e69f` | fix: unwrap success_response envelope in GlobalConfigService credential fetchers |
| `a85ca3d` | fix: allow public access to fixed OAuth callback endpoints |
| `29d14b1` | fix: don't require user token on the public Google Calendar OAuth callback |
| `9eeef75` | feat: redirect Google Calendar OAuth callback back to CRM frontend |
| `50c1b24` | fix: mark google_calendar integration as connected after OAuth completes |
| `40f7215` | fix(google-calendar): respect saved business hours and serialize set values in chat history |
| `e35850f` | fix(google-calendar): fix invalid RFC3339 timestamps and add update/delete event tools |
| `3ce94a1` | feat(google-calendar): support native event reminders via create_calendar_event |
| `38a48e1` | fix(google-calendar): stop injecting reminder_minutes_before guidance for all agents |
| `5b354f9` | feat(chat): support contact_id in test chat for CRM context injection |

### `evo-ai-frontend-community` (branch `production`, fork `agbid`)

| Commit | Mensagem |
|---|---|
| `ab01ccb` | feat: handle Google Calendar OAuth redirect and add fork CI for ghcr.io/agbid |
| `8dcf5c1` | fix: normalize agent-integrations response shape (object vs array) |
| `b698022` | fix(google-calendar): unwrap response envelope when fetching calendar list |
| `90c01f4` | fix(chat): surface tool error messages in agent test chat |
| `54e3a29` | feat(agents): add contact selector to agent test chat |

### `evo-ai-crm-community` (branch `fix/contacts-conversations-endpoint`, fork `agbid`, PR aberto)

| Commit | Mensagem |
|---|---|
| `2c217d7` | fix(contacts): render conversations list for GET /contacts/:id/conversations |

### Alteração de instrução (sem commit — `evo_core_agents.instruction` em produção)

| Agente | O que mudou |
|---|---|
| `inter_rural_atendimento` (`afbeaa63-eeb3-418e-901d-5eacc32fe75c`) | Parágrafo "SEU OBJETIVO É AGENDAR REUNIÕES" passou a exigir `check_calendar_availability(find_slots=true)` antes de sugerir/validar horários — ver Fix 16 |
