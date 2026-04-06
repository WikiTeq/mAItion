# mAItion

![mAItion](https://github.com/WikiTeq/mAItion/blob/main/mAItion.png?raw=true)

mAItion is an all-in-one ready-to-use AI-powered tool that combines your existing knowledge with LLMs, 
allowing you to chat, search and interact with your data through a slick chat interface. With mAItion
you can aggregate all your knowledge from many sources using Connectors into a central place and 
interact with your knowledge with ease!

## ✨ Features

* Support for both local and remote LLMs for embedding and inference
* Asynchronous data ingestion with deduplication and per-source configurable schedules
* Data ingestion from S3 buckets with Everything-to-Markdown conversion via [MarkItDown](https://github.com/microsoft/markitdown)
* Data ingestion from MediaWiki with Wiki-to-Markdown conversion via [html2text](https://github.com/Alir3z4/html2text)
* SerpAPI search query results ingestion from Google Search results with customizable queries
* Flexible configuration supporting an arbitrary number of connectors
* Built with extensibility in mind, allowing for custom connectors addition with ease
* MCP servers support (stdio, streamable http)
* Web-search support (through external services and via on-premise services)
* In-place chat with uploaded documents and images (for multi-modal LLMs)
* Code execution and Code interpreter
* Text-to-Speech and Speech-to-Text capabilities
* Image generator (requires model supporting image generation)
* Flexible automation capabilities through Functions and Pipelines
* Multi-user setup with fine-grained groups and permissions
* Support for multiple customized configurations of LLM models

## ✨ Use-cases

* A single place to chat with your company knowledge that's scattered across many external systems
* A central space for looking up and refining your existing knowledge across many knowledge bases
* A tool to find secret knowledge that can not be found in the other was across your scattered data
* An entry-point into your on-premise hosted LLM models supporting evaluations and per-model settings

### 🌐 Connectors included

* S3 (any AWS compatible Object Storage including AWS, Contabo, B2, Cloudflare R2, OVH, etc)
* MediaWiki (all versions supported, both private and public wiki)
* SerpAPI
* GitHub (repository files and issues, PAT or GitHub App auth)
* GitLab (repository files and issues, supports GitLab.com and self-hosted)

### 🌐 Extra connectors

Over 100 extra connectors are available at request, including the most popular ones:

* Gmail
* Google Drive
* Jira
* Slack
* Notion
* Microsoft Teams
* Microsoft Office 365
* Dropbox
* Trello
* YouTube
* FTP
* SCP
* SSH
* and many more..

## Quick start

### Requirements

* Docker and Docker Compose
* OpenRouter or OpenAI API key (a free OpenRouter account works with the default configuration)
* S3 bucket

### Setup

* Create `.env.rag` file by copying `.env.rag.example` (see https://github.com/wikiteq/rag-of-all-trades for details)
    * Set `OPENROUTER_API_KEY`
    * Set `S3_ACCOUNT1_*` values to match your source S3 bucket with files
* Create `config.yaml` out of `config.yaml.example`
    * The default config works OK and is configured to:
        * Use a single S3 bucket as data source
        * Use `openai/gpt-oss-20b:free` [model](https://openrouter.ai/openai/gpt-oss-20b:free) for rerphrase
        * User local `sentence-transformers/all-mpnet-base-v2` [model](https://huggingface.co/sentence-transformers/all-mpnet-base-v2) for embeddings
        * You can change the values if necessary, refer to https://github.com/wikiteq/rag-of-all-trades for details
* Create `.env` file by copying `.env.openwebui.example`
    * Set `OPENAI_API_KEY`
    * Optionally set `OPENAI_DEFAULT_MODEL`

Start the stack by running `docker compose up -d`. Wait until all the services
become healthy. You can check health status by running `docker compose ps` and checking
the `STATUS` column of the services:

```bash
docker compose ps
NAME                                      IMAGE                                      COMMAND                  SERVICE     CREATED         STATUS                   PORTS
rag-of-all-trades-openwebui-api-1         ghcr.io/wikiteq/rag-of-all-trades:latest   "sh -c 'alembic upgr…"   api         4 minutes ago   Up 4 minutes (healthy)   8000/tcp
rag-of-all-trades-openwebui-openwebui-1   ghcr.io/open-webui/open-webui:0.6.5        "/custom-entrypoint.…"   openwebui   4 minutes ago   Up 4 minutes (healthy)   0.0.0.0:3000->8080/tcp, [::]:3000->8080/tcp
rag-of-all-trades-openwebui-postgres-1    ankane/pgvector:v0.5.1                     "docker-entrypoint.s…"   postgres    4 minutes ago   Up 4 minutes (healthy)   5432/tcp
rag-of-all-trades-openwebui-redis-1       redis:7                                    "docker-entrypoint.s…"   redis       4 minutes ago   Up 4 minutes (healthy)   6379/tcp
```

It takes up to a minute for the OpenWebUI to fully boot on cold start.

Once all the services are booted and report healthy status visit http://localhost:3000 and
login using Admin credentials. The credentials are defined in `X_WEBUI_ADMIN_EMAIL` and `X_WEBUI_ADMIN_PASS`
of the `.env` file. The default ones are:

* username: `admin@example123.com`
* password: `q1w2e3r4!`

If you did not change the `ENABLE_OPENAI_API` you will also have LLM provider
pre-configured with the values you have in the `.env` including the default chat model

The filter function that's responsible for the RAG service communication will also be
automatically provisioned and enabled globally. You can change these settings at the Admin panel

## Connectors configuration

The service supports multiple data sources, including multiple data sources of the same type, each with its own
ingestion schedule. The connectors to enable are defined via `config.yaml`, and their secrets are defined
in the `.env.rag` file.

### S3 Connector

The S3 connector ingests documents from S3 buckets and converts them to Markdown format.
The connector has the following configuration options:

```yaml
# config.yaml

sources:
  - 
  - type: "s3" # must be s3
    name: "account1" # arbitrary name for the connector, will be stored in metadata
    config:
      endpoint: "${S3_ACCOUNT1_ENDPOINT}" # s3 endpoint
      access_key: "${S3_ACCOUNT1_ACCESS_KEY}" # s3 access key
      secret_key: "${S3_ACCOUNT1_SECRET_KEY}" # s3 secret key
      region: "${S3_ACCOUNT1_REGION}" # s3 region
      use_ssl: "${S3_ACCOUNT1_USE_SSL}" # use ssl for s3 connection, can be True or False
      buckets: "${S3_ACCOUNT1_BUCKETS}" # single entry or comma-separated list i.e. bucket1,bucket2
      schedules: "${S3_ACCOUNT1_SCHEDULES}" # single entry or comma-separated list i.e. 3600,60
      
  - type: "s3"
    name: "account2"
    config:
      ...

  - type: "s3"
    name: "account3"
    config:
      ...
```

````dotenv
# .env.rag

S3_ACCOUNT1_ENDPOINT=https://s3.amazonaws.com
S3_ACCOUNT1_ACCESS_KEY=xxx
S3_ACCOUNT1_SECRET_KEY=xxx
S3_ACCOUNT1_REGION=us-east-1
S3_ACCOUNT1_USE_SSL=True
S3_ACCOUNT1_BUCKETS=bucket1,bucket2
S3_ACCOUNT1_SCHEDULES=3600,60
````

### MediaWiki Connector

The MediaWiki connector ingests documents from MediaWiki sites and converts them to Markdown format.
The connector has the following configuration options:

```yaml
# config.yaml

sources:
  - type: "mediawiki"
    name: "wiki1"
    config:
      api_url: "${MEDIAWIKI1_API_URL}"
      request_delay: 0.1
      schedules: "${MEDIAWIKI1_SCHEDULES}"

  - type: "mediawiki"
    name: "wiki2"
    config:
      ...

  - type: "mediawiki"
    name: "wiki3"
    config:
      ...
```

```dotenv
# .env.rag

MEDIAWIKI1_API_URL=https://en.wikipedia.org/w/api.php
MEDIAWIKI1_SCHEDULES=3600
````

### SerpAPI Connector

The SerpAPI connector ingests documents from Google Search results and converts them to Markdown format.
The connector has the following configuration options:

```yaml
# config.yaml

sources:
  - type: "serpapi"
    name: "serp_ingestion1"
    config:
      api_key: "${SERPAPI1_KEY}"
      queries: "${SERPAPI1_QUERIES}"
      schedules: "${SERPAPI1_SCHEDULES}"

  - type: "serpapi"
    name: "serp_ingestion2"
    config:

  - type: "serpapi"
    name: "serp_ingestion3"
    config:
```

```dotenv
# .env.rag

SERPAPI1_KEY=xxxx
SERPAPI1_QUERIES=aaa
SERPAPI1_SCHEDULES=3600
````

### GitHub Connector

The GitHub connector ingests repository files and optionally issues from a GitHub repository.
Supports PAT and GitHub App authentication, branch or commit targeting, file extension/directory
filters, and issue label filters.

```yaml
# config.yaml

sources:
  - type: "github"
    name: "github1"
    config:
      # Auth — use one of: personal_token OR github_app_* credentials
      personal_token: "${GITHUB1_PERSONAL_TOKEN}"
      owner: "${GITHUB1_OWNER}"          # repository owner / org
      repo: "${GITHUB1_REPO}"            # repository name
      branch: "main"                     # default "main" (mutually exclusive with commit_sha)
      include_extensions: "md,py"        # optional, comma-separated
      include_issues: false              # set true to also ingest issues
      concurrent_requests: 5             # optional, default 5
      schedules: "${GITHUB1_SCHEDULES}"
```

```dotenv
# .env.rag

GITHUB1_PERSONAL_TOKEN=ghp_xxxxxxxxxxxx
GITHUB1_OWNER=your-org-or-username
GITHUB1_REPO=your-repo-name
GITHUB1_SCHEDULES=3600
```

For GitHub App authentication, replace `personal_token` with:

```yaml
      github_app_id: "${GITHUB1_APP_ID}"
      github_app_installation_id: "${GITHUB1_APP_INSTALLATION_ID}"
      github_app_private_key: "${GITHUB1_APP_PRIVATE_KEY}"
```

### GitLab Connector

The GitLab connector ingests repository files and optionally issues from a GitLab project or group.
Supports GitLab.com and self-hosted instances via a Personal Access Token with `read_api` scope.

```yaml
# config.yaml

sources:
  - type: "gitlab"
    name: "gitlab1"
    config:
      gitlab_url: "${GITLAB1_URL}"
      personal_token: "${GITLAB1_TOKEN}"
      project_id: 12345678              # integer project ID (required unless group_id only)
      #group_id: 999                    # optional, for group-level issue queries
      ref: "main"                       # optional, branch/tag/commit, default "main"
      #path: "docs"                     # optional, limit to sub-directory
      #file_path: "README.md"           # optional, single file only
      recursive: true                   # optional, default true
      include_issues: false             # set true to also ingest issues
      #issues_state: "opened"           # optional: opened/closed/all, default "opened"
      #issues_labels: "bug,docs"        # optional, comma-separated
      #issues_assignee: "username"      # optional
      #issues_author: "username"        # optional
      #issues_milestone: "v1.0"         # optional
      #issues_search: "keyword"         # optional
      #issues_get_all: false            # optional, fetch all pages, default false
      #issues_scope: "created_by_me"    # optional: created_by_me/assigned_to_me/all
      #issues_type: "issue"             # optional: issue/incident/test_case/task
      #issues_confidential: false       # optional
      #issues_iids: [1, 2, 3]           # optional, filter by specific issue IDs
      #issues_created_after: "2024-01-01T00:00:00Z"
      #issues_created_before: "2024-12-31T23:59:59Z"
      schedules: "${GITLAB1_SCHEDULES}"
```

```dotenv
# .env.rag

GITLAB1_URL=https://gitlab.com
GITLAB1_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB1_SCHEDULES=3600
```

## Embeddings and Inference

### Embeddings support

Both local and remote OpenAI-compatible models are supported for embeddings:

* `Local` (running arbitrary embedding models from HuggingFace)
* `OpenRouter`
* `OpenAI` or `OpenAI`-compatible

### Inference support

Both local and remote OpenAI-compatible models are supported for inference:

* `OpenRouter`
* `OpenAI` or `OpenAI`-compatible

### Embeddings-only HuggingFace local model

You can configure the service to use **local embeddings** only, in this mode
you can use any embedding model supported by HuggingFace. Inference is disabled in
this mode, so you won't be able to use the **rephrase** endpoint.

```yaml
# config.yaml

embedding:
  provider: local
  # you can use any embedding model supported by HuggingFace
  model_config: sentence-transformers/all-MiniLM-L6-v2
  embedding_dim: 384

inference:
  provider: None
  model_config: None
```

### Embeddings-only OpenRouter/OpenAI model

You can configure the service to use **remote embeddings**, in this mode
you can use any embedding model supported by OpenRouter/OpenAI. Inference is disabled in
this mode, so you won't be able to use the **rephrase** endpoint.

```yaml
# config.yaml

embedding:
  provider: openrouter
  model_config: text-embedding-3-small
  embedding_dim: 1536

inference:
  provider: None
  model_config: None
```

You must set `OPENROUTER_API_KEY` and `OPENROUTER_API_BASE` in the `.env.rag` file.

### Embeddings and inference OpenRouter/OpenAI model

You can configure the service to use **remote embeddings** and **remote inference**, in this mode
you can use any embedding and inference models supported by OpenRouter/OpenAI.

```yaml
# config.yaml

embedding:
  provider: openrouter
  model_config: text-embedding-3-small
  embedding_dim: 1536

inference:
  provider: openrouter
  model_config: gpt-4o
```

You must set `OPENROUTER_API_KEY` and `OPENROUTER_API_BASE` in the `.env.rag` file.

## Reference of the `config.yaml`

The `config.yaml` file contains the main configuration of the service.

> Environment variables (`${...}`) in the config file are evaluated at runtime.

```yaml
sources: # holds the list of sources to ingest from (Connectors)

  - type: # type of the connector (s3, mediawiki, serpapi)
    name: # arbitrary name for the connector, will be stored in metadata
    config:
      # connector specific configuration
      schedules: "${S3_ACCOUNT1_SCHEDULES}"

# configures models and dimensions for embeddings
embedding:
  provider: openrouter # `openrouter`/`openai` or `local` for local HuggingFace embeddings
  model_config: text-embedding-3-small # model to use
  embedding_dim: 1536 # dimensions (check with the model docs)

# configures the LLM provider and model
inference:
  provider: openrouter # `openrouter`/`openai`
  model_config: gpt-4o # model to use

# vector store configuration
vector_store:
  table_name: embeddings
  hybrid_search: true # whether to use hybrid search or not
  chunk_size: 512 # chunk size for vector indexing
  chunk_overlap: 50 # overlap between chunks
  # hnsw indexes settings
  hnsw:
    hnsw_m: 16 # number of neighbors
    hnsw_ef_construction: 64 # ef construction parameter for HNSW
    hnsw_ef_search: 40 # ef search parameter for HNSW
    hnsw_dist_method: vector_cosine_ops # distance metric for HNSW
```

## Tech Stack

* [Rag-Of-All-Trades](https://github.com/wikiteq/rag-of-all-trades) v0.1 as a RAG backend
* [OpenWebUI](https://github.com/open-webui/open-webui) v0.6.5 as a front-end

## Troubleshooting

### OpenWebUI does not start

The `openwebui` service depends on the `api` service healthiness and will remain pending until the API service
is online. Check the `api` container for any errors, review the `config.yaml` and `.env.rag` for typos.

### HuggingFace connection timeout

```
requests.exceptions.ReadTimeout: (ReadTimeoutError("HTTPSConnectionPool(host='huggingface.co', port=443): Read timed out. (read timeout=10)"), '(Request ID: da122313-e11f-4d54-b4f3-187abfea0ca3)')
```

OpenWebUI downloads some HuggingFace models during first boot. Sometimes HuggingFace endpoints may timeout.
In this scenario just run `docker compose down -v` to wipe the stack and start over with `docker compose up -d`
