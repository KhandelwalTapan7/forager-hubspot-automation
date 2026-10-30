# Forager x HubSpot enrichment

FastAPI service that enriches HubSpot companies and contacts from Forager and finds contacts by job title.

## Endpoints

All except `/health` and the webhook need header `X-API-KEY`.

| Method | Path | Body |
| --- | --- | --- |
| POST | `/enrich/company` | `{"company_id": "..."}` or `{"domain": "openai.com"}` |
| POST | `/enrich/contact` | `{"contact_id": "..."}` |
| POST | `/find-contacts` | `{"domain": "openai.com", "titles": ["CTO", "VP Engineering"], "limit": 5}` |
| POST | `/webhooks/hubspot` | HubSpot `company.creation` and `contact.creation` events, v3 signature verified |

Responses include the Forager extras (headline, About, skills) for discussion. Set `INCLUDE_EXTRAS=1` to also write the About text to HubSpot's LinkedIn bio field.

## Behaviour

- Title filtering matches whole words, expands abbreviations (CTO / Chief Technology Officer, VP / Vice President) and drops assistant and intern roles.
- Records written by this service carry `forager_enriched=true`, so their own creation webhooks are skipped and credits are never spent twice.
- Contacts already in HubSpot are skipped during contact search.
- Forager 402 (no credits) is returned as a 402 response.

## Setup

1. Copy `.env.example` to `.env` and fill it in. Set the same variables in Railway.
2. HubSpot service key scopes: contacts and companies read/write, plus schemas write for both. Custom properties are created on startup.
3. Run locally: `pip install -r requirements.txt && uvicorn main:app --env-file .env`.
4. Deploy: connect the repo to Railway; the `Procfile` is the start command.
5. Webhooks: `hubspot-app/` is a HubSpot developer-platform project. Set `targetUrl` in `src/app/webhooks/webhooks-hsmeta.json` to your Railway URL, then run `hs project upload` from that folder. Put the app's client secret in `HUBSPOT_CLIENT_SECRET`.
