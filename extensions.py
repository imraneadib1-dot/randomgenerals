"""Skills, connectors and plugins: what the AI can be extended with.

THREE THINGS, ONE CATALOGUE

  SKILLS      Expert instructions for one kind of job - slides, a
              spreadsheet, a code review. A skill switches on by itself
              when a message matches it, or by name with /skill-name.
              Built-in ones live here; people can write their own
              (stored in the database, same shape).

  CONNECTORS  Other apps the AI can use, over MCP (mcp_client.py). The
              gallery below lists apps whose servers were checked to
              support one-click sign-in - or need no sign-in at all - so
              "Connect" really is one click.

  PLUGINS     Bundles: a few skills that only make sense together, plus
              the apps they work with. Installing one turns its skills on
              and puts its apps at the top of the Connectors list.

Only data and the matching logic live here. Storage, routes and the
chat-turn wiring are in app.py and db.py.
"""
import os
import re

# ----------------------------------------------------------------------
# CONNECTOR GALLERY
#
# auth:  "none"   - no sign-in; connecting just saves it
#        "oauth"  - one-click sign-in with dynamic registration (checked
#                   against each server when this list was written)
#        "allowlist" - the app's sign-in only accepts services it has
#                   approved by hand (it refused RandomGenerals' registration
#                   or redirect address when tested). Kept here so it is
#                   clear why it is missing; never shown.
#        "app"    - one-click for users once the site owner has
#                   registered RandomGenerals with that service and set
#                   MCP_<ID>_CLIENT_ID / MCP_<ID>_CLIENT_SECRET. Hidden
#                   until then, because a button that cannot work is
#                   worse than no button.
#
# `words` are what make an app's tools appear in a chat turn: its tools
# are offered only when the conversation mentions it, so connecting six
# apps does not put two hundred tool descriptions in every request.
# ----------------------------------------------------------------------
APPS = [
    # productivity
    {"id": "notion", "logo": "notion.svg", "title": "Notion", "url": "https://mcp.notion.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#191919",
     "description": "Search, read and write pages and databases in your workspace.",
     "words": ["notion"]},
    {"id": "linear", "logo": "linear.svg", "title": "Linear", "url": "https://mcp.linear.app/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#5e6ad2",
     "description": "Find, create and update issues, projects and cycles.",
     "words": ["linear", "issue", "issues", "ticket", "tickets"]},
    {"id": "atlassian", "logo": "atlassian.svg", "title": "Jira & Confluence", "url": "https://mcp.atlassian.com/v1/mcp",
     "auth": "oauth", "category": "Productivity", "color": "#0c66e4",
     "description": "Work with Jira issues and Confluence pages.",
     "words": ["jira", "confluence", "atlassian"]},
    {"id": "monday", "logo": "monday.png", "title": "monday.com", "url": "https://mcp.monday.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#ff3d57",
     "description": "Read and update boards, items and updates.",
     "words": ["monday", "board", "boards"]},
    {"id": "asana", "logo": "asana.svg", "title": "Asana", "url": "https://mcp.asana.com/v2/mcp", "auth": "app",
     "category": "Productivity", "color": "#f06a6a",
     "description": "Tasks, projects and goals in Asana.", "words": ["asana"]},
    {"id": "intercom", "title": "Intercom", "url": "https://mcp.intercom.com/mcp", "auth": "allowlist",
     "category": "Productivity", "color": "#1f8ded",
     "description": "Search conversations and contacts.", "words": ["intercom", "support inbox"]},
    {"id": "zapier", "logo": "zapier.ico", "title": "Zapier", "url": "https://mcp.zapier.com/api/mcp/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#ff4f00",
     "description": "Run the actions you have set up in Zapier across 8,000 apps.",
     "words": ["zapier", "zap", "gmail", "google sheets", "slack", "calendar"]},
    {"id": "box", "logo": "box.svg", "title": "Box", "url": "https://mcp.box.com", "auth": "app",
     "category": "Productivity", "color": "#0061d5",
     "description": "Search and read files in Box.", "words": ["box.com", "box files", "my box"]},
    # developer
    {"id": "github", "logo": "github.svg", "title": "GitHub", "url": "https://api.githubcopilot.com/mcp/", "auth": "app",
     "category": "Developer", "color": "#24292f",
     "description": "Repositories, issues, pull requests and code search.",
     "words": ["github", "repo", "repository", "pull request", "pr "]},
    {"id": "sentry", "logo": "sentry.svg", "title": "Sentry", "url": "https://mcp.sentry.dev/mcp", "auth": "oauth",
     "category": "Developer", "color": "#362d59",
     "description": "Look up errors, issues and releases.", "words": ["sentry", "error report", "crash"]},
    {"id": "vercel", "title": "Vercel", "url": "https://mcp.vercel.com", "auth": "allowlist",
     "category": "Developer", "color": "#000000",
     "description": "Projects, deployments and build logs.", "words": ["vercel", "deployment", "deploy"]},
    {"id": "cloudflare", "logo": "cloudflare.svg", "title": "Cloudflare", "url": "https://bindings.mcp.cloudflare.com/mcp",
     "auth": "oauth", "category": "Developer", "color": "#f38020",
     "description": "Workers, KV, R2 and D1 in your Cloudflare account.",
     "words": ["cloudflare", "worker", "workers", "r2", "d1"]},
    {"id": "context7", "logo": "context7.png", "title": "Context7", "url": "https://mcp.context7.com/mcp", "auth": "none",
     "category": "Developer", "color": "#059669",
     "description": "Up-to-date documentation and examples for thousands of libraries.",
     "words": ["docs", "documentation", "library", "framework", "api reference", "context7"]},
    {"id": "deepwiki", "logo": "deepwiki.ico", "title": "DeepWiki", "url": "https://mcp.deepwiki.com/mcp", "auth": "none",
     "category": "Developer", "color": "#2563eb",
     "description": "Ask questions about any public GitHub repository.",
     "words": ["deepwiki", "github.com/", "open source", "repo", "repository"]},
    {"id": "cfdocs", "logo": "cfdocs.svg", "title": "Cloudflare Docs", "url": "https://docs.mcp.cloudflare.com/mcp",
     "auth": "none", "category": "Developer", "color": "#f38020",
     "description": "Search Cloudflare's documentation.", "words": ["cloudflare"]},
    {"id": "huggingface", "logo": "huggingface.svg", "title": "Hugging Face", "url": "https://huggingface.co/mcp", "auth": "none",
     "category": "Developer", "color": "#ffbd2e",
     "description": "Find models, datasets, Spaces and papers.",
     "words": ["hugging face", "huggingface", "model", "dataset", "paper"]},
    # design & web
    {"id": "figma", "title": "Figma", "url": "https://mcp.figma.com/mcp", "auth": "allowlist",
     "category": "Design", "color": "#a259ff",
     "description": "Read designs, components and variables from your files.", "words": ["figma"]},
    {"id": "canva", "title": "Canva", "url": "https://mcp.canva.com/mcp", "auth": "allowlist",
     "category": "Design", "color": "#00c4cc",
     "description": "Search, create and export Canva designs.", "words": ["canva"]},
    {"id": "webflow", "logo": "webflow.svg", "title": "Webflow", "url": "https://mcp.webflow.com/mcp", "auth": "oauth",
     "category": "Design", "color": "#146ef5",
     "description": "Manage Webflow sites, pages and CMS items.", "words": ["webflow"]},
    {"id": "wix", "logo": "wix.svg", "title": "Wix", "url": "https://mcp.wix.com/mcp", "auth": "oauth",
     "category": "Design", "color": "#0c6efc",
     "description": "Manage your Wix sites and business data.", "words": ["wix"]},
    # payments
    {"id": "stripe", "logo": "stripe.svg", "title": "Stripe", "url": "https://mcp.stripe.com", "auth": "oauth",
     "category": "Payments", "color": "#635bff",
     "description": "Customers, payments, invoices and products.",
     "words": ["stripe", "payment", "payments", "invoice", "refund", "subscription"]},
    {"id": "paypal", "logo": "paypal.svg", "title": "PayPal", "url": "https://mcp.paypal.com/mcp", "auth": "oauth",
     "category": "Payments", "color": "#003087",
     "description": "Invoices, orders and transactions.", "words": ["paypal"]},
    {"id": "square", "title": "Square", "url": "https://mcp.squareup.com/mcp", "auth": "allowlist",
     "category": "Payments", "color": "#3e4348",
     "description": "Orders, catalog, customers and payments.", "words": ["square"]},
    {"id": "hubspot", "logo": "hubspot.svg", "title": "HubSpot", "url": "https://mcp.hubspot.com/", "auth": "app",
     "category": "Payments", "color": "#ff7a59",
     "description": "Contacts, companies and deals in your CRM.",
     "words": ["hubspot", "crm", "deal", "deals", "contacts"]},
    # search
    {"id": "exa", "logo": "exa.png", "title": "Exa Search", "url": "https://mcp.exa.ai/mcp", "auth": "none",
     "category": "Research", "color": "#1f40ed",
     "description": "Deep web search and page reading.",
     "words": ["exa", "research", "search the web", "find articles"]},

    # ---- added after a second round of testing: each one either answered
    # a real tool call with no sign-in, or accepted RandomGenerals'
    # registration AND opened its real sign-in page for the live callback.
    # `words_only` is for names that are everyday words (Close, Ramp,
    # Apollo, Railway...): only the listed phrases load their tools.
    # productivity
    {"id": "airtable", "logo": "airtable.svg", "title": "Airtable", "url": "https://mcp.airtable.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#18bfff",
     "description": "Read and update records in your bases and tables.", "words": ["airtable"]},
    {"id": "clickup", "logo": "clickup.svg", "title": "ClickUp", "url": "https://mcp.clickup.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#7b68ee",
     "description": "Tasks, lists, docs and time tracking.", "words": ["clickup"]},
    {"id": "todoist", "logo": "todoist.svg", "title": "Todoist", "url": "https://ai.todoist.net/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#e44332",
     "description": "Add, find and complete your tasks and projects.",
     "words": ["todoist", "my tasks", "to-do list", "todo list"]},
    {"id": "miro", "logo": "miro.svg", "title": "Miro", "url": "https://mcp.miro.com/", "auth": "oauth",
     "category": "Productivity", "color": "#ffd02f",
     "description": "Read and create content on your Miro boards.", "words": ["miro", "whiteboard"]},
    {"id": "coda", "logo": "coda.svg", "title": "Coda", "url": "https://coda.io/apis/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#f46a54",
     "description": "Search and edit your Coda docs and tables.", "words": ["coda", "coda doc"], "words_only": True},
    {"id": "basecamp", "logo": "basecamp.svg", "title": "Basecamp", "url": "https://mcp.basecamp.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#1d2d35",
     "description": "Projects, to-dos, messages and schedules.", "words": ["basecamp"]},
    {"id": "calendly", "logo": "calendly.svg", "title": "Calendly", "url": "https://mcp.calendly.com", "auth": "oauth",
     "category": "Productivity", "color": "#006bff",
     "description": "Event types, bookings and availability.", "words": ["calendly", "booking", "bookings"]},
    {"id": "calcom", "logo": "calcom.svg", "title": "Cal.com", "url": "https://mcp.cal.com/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#111827",
     "description": "Bookings and availability in Cal.com.", "words": ["cal.com"], "words_only": True},
    {"id": "granola", "logo": "granola.png", "title": "Granola", "url": "https://mcp.granola.ai/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#3f6b3a",
     "description": "Search your meeting notes and transcripts.",
     "words": ["granola", "meeting notes", "my meetings"]},
    {"id": "fireflies", "logo": "fireflies.png", "title": "Fireflies", "url": "https://api.fireflies.ai/mcp", "auth": "oauth",
     "category": "Productivity", "color": "#7b2cbf",
     "description": "Meeting transcripts, summaries and action items.",
     "words": ["fireflies", "fireflies.ai", "meeting transcript", "call transcript"], "words_only": True},
    {"id": "jotform", "logo": "jotform.png", "title": "Jotform", "url": "https://mcp.jotform.com/", "auth": "oauth",
     "category": "Productivity", "color": "#ff6100",
     "description": "Forms and their submissions.", "words": ["jotform", "form submissions"]},
    # developer
    {"id": "gitlab", "logo": "gitlab.svg", "title": "GitLab", "url": "https://gitlab.com/api/v4/mcp", "auth": "oauth",
     "category": "Developer", "color": "#fc6d26",
     "description": "Projects, issues, merge requests and pipelines.", "words": ["gitlab", "merge request"]},
    {"id": "supabase", "logo": "supabase.svg", "title": "Supabase", "url": "https://mcp.supabase.com/mcp", "auth": "oauth",
     "category": "Developer", "color": "#3ecf8e",
     "description": "Your Supabase projects: tables, SQL, functions and logs.", "words": ["supabase"]},
    {"id": "neon", "logo": "neon.png", "title": "Neon", "url": "https://mcp.neon.tech/mcp", "auth": "oauth",
     "category": "Developer", "color": "#00e599",
     "description": "Serverless Postgres: projects, branches and SQL.",
     "words": ["neon database", "neon db", "neon postgres", "neon.tech"], "words_only": True},
    {"id": "netlify", "logo": "netlify.svg", "title": "Netlify", "url": "https://netlify-mcp.netlify.app/mcp", "auth": "oauth",
     "category": "Developer", "color": "#00ad9f",
     "description": "Sites, deploys and environment settings.", "words": ["netlify"]},
    {"id": "railway", "logo": "railway.svg", "title": "Railway", "url": "https://mcp.railway.com", "auth": "oauth",
     "category": "Developer", "color": "#0b0d0e",
     "description": "Projects, services, deployments and logs.",
     "words": ["railway app", "railway.app", "railway deploy", "on railway", "railway project"], "words_only": True},
    {"id": "prisma", "logo": "prisma.svg", "title": "Prisma Postgres", "url": "https://mcp.prisma.io/mcp", "auth": "oauth",
     "category": "Developer", "color": "#2d3748",
     "description": "Create and manage Prisma Postgres databases.", "words": ["prisma"]},
    {"id": "planetscale", "logo": "planetscale.svg", "title": "PlanetScale", "url": "https://mcp.pscale.dev/mcp/planetscale", "auth": "oauth",
     "category": "Developer", "color": "#000000",
     "description": "Databases, branches and queries.", "words": ["planetscale"]},
    {"id": "postman", "logo": "postman.svg", "title": "Postman", "url": "https://mcp.postman.com/mcp", "auth": "oauth",
     "category": "Developer", "color": "#ff6c37",
     "description": "Collections, requests and API specs.", "words": ["postman", "api collection"]},
    {"id": "cloudinary", "logo": "cloudinary.svg", "title": "Cloudinary", "url": "https://asset-management.mcp.cloudinary.com/mcp",
     "auth": "oauth", "category": "Developer", "color": "#3448c5",
     "description": "Upload, find and transform images and videos.", "words": ["cloudinary"]},
    {"id": "apify", "logo": "apify.png", "title": "Apify", "url": "https://mcp.apify.com", "auth": "oauth",
     "category": "Developer", "color": "#97d700",
     "description": "Run thousands of ready-made scrapers and automations.", "words": ["apify", "scraper", "scrape"]},
    {"id": "cfbrowser", "logo": "cfbrowser.svg", "title": "Cloudflare Browser", "url": "https://browser.mcp.cloudflare.com/mcp",
     "auth": "oauth", "category": "Developer", "color": "#f38020",
     "description": "Fetch pages and take screenshots with a real browser.",
     "words": ["screenshot of", "render the page", "cloudflare browser"], "words_only": True},
    {"id": "awsdocs", "logo": "awsdocs.png", "title": "AWS Docs", "url": "https://knowledge-mcp.global.api.aws", "auth": "none",
     "category": "Developer", "color": "#232f3e",
     "description": "Search and read the official AWS documentation.",
     "words": ["aws", "amazon web services", "s3", "lambda", "ec2", "dynamodb", "cloudformation"], "words_only": True},
    {"id": "mslearn", "logo": "mslearn.png", "title": "Microsoft Learn", "url": "https://learn.microsoft.com/api/mcp", "auth": "none",
     "category": "Developer", "color": "#0078d4",
     "description": "Official Microsoft and Azure docs and code samples.",
     "words": ["azure", "microsoft docs", "microsoft learn", ".net", "c#", "powershell", "power bi"], "words_only": True},
    {"id": "gitmcp", "logo": "gitmcp.svg", "title": "GitMCP", "url": "https://gitmcp.io/docs", "auth": "none",
     "category": "Developer", "color": "#24292f",
     "description": "Docs and code from any public GitHub project.",
     "words": ["gitmcp", "github.com/"], "words_only": True},
    # data & analytics
    {"id": "posthog", "logo": "posthog.svg", "title": "PostHog", "url": "https://mcp.posthog.com/mcp", "auth": "oauth",
     "category": "Data & analytics", "color": "#1d4aff",
     "description": "Product analytics, feature flags and session insights.", "words": ["posthog", "feature flag"]},
    {"id": "mixpanel", "logo": "mixpanel.svg", "title": "Mixpanel", "url": "https://mcp.mixpanel.com/mcp", "auth": "oauth",
     "category": "Data & analytics", "color": "#7856ff",
     "description": "Query events, funnels and retention.", "words": ["mixpanel"]},
    {"id": "amplitude", "logo": "amplitude.png", "title": "Amplitude", "url": "https://mcp.amplitude.com/mcp", "auth": "oauth",
     "category": "Data & analytics", "color": "#1e61f0",
     "description": "Charts, cohorts and product analytics.",
     "words": ["amplitude analytics", "in amplitude", "amplitude chart", "amplitude cohort"], "words_only": True},
    # sales & marketing
    {"id": "mailchimp", "logo": "mailchimp.svg", "title": "Mailchimp", "url": "https://mcp.mailchimp.com/mcp", "auth": "oauth",
     "category": "Sales & marketing", "color": "#ffe01b",
     "description": "Audiences, campaigns and their results.", "words": ["mailchimp", "email campaign"]},
    {"id": "klaviyo", "logo": "klaviyo.png", "title": "Klaviyo", "url": "https://mcp.klaviyo.com/mcp", "auth": "oauth",
     "category": "Sales & marketing", "color": "#232426",
     "description": "Email and SMS campaigns, flows and metrics.", "words": ["klaviyo"]},
    {"id": "apollo", "logo": "apollo.png", "title": "Apollo.io", "url": "https://mcp.apollo.io/mcp", "auth": "oauth",
     "category": "Sales & marketing", "color": "#1c1c1c",
     "description": "Find people and companies, and enrich leads.",
     "words": ["apollo.io", "apollo leads", "prospects", "find leads"], "words_only": True},
    {"id": "close", "logo": "close.png", "title": "Close CRM", "url": "https://mcp.close.com/mcp", "auth": "oauth",
     "category": "Sales & marketing", "color": "#1463ff",
     "description": "Leads, opportunities and activities in Close.",
     "words": ["close crm", "close.com"], "words_only": True},
    # finance
    {"id": "ramp", "logo": "ramp.png", "title": "Ramp", "url": "https://mcp.ramp.com/mcp", "auth": "oauth",
     "category": "Payments", "color": "#e4f222",
     "description": "Cards, transactions, bills and reimbursements.",
     "words": ["ramp card", "ramp transactions", "ramp expenses", "in ramp"], "words_only": True},
    {"id": "mercury", "logo": "mercury.png", "title": "Mercury", "url": "https://mcp.mercury.com/mcp", "auth": "oauth",
     "category": "Payments", "color": "#5266eb",
     "description": "Bank accounts, balances and transactions.",
     "words": ["mercury bank", "mercury account", "mercury balance"], "words_only": True},
    # research & web
    {"id": "tavily", "logo": "tavily.png", "title": "Tavily", "url": "https://mcp.tavily.com/mcp", "auth": "oauth",
     "category": "Research", "color": "#2b5ce6",
     "description": "Web search built for AI, with extraction and crawling.", "words": ["tavily"]},
    {"id": "parallel", "logo": "parallel.png", "title": "Parallel Search", "url": "https://search-mcp.parallel.ai/mcp", "auth": "oauth",
     "category": "Research", "color": "#111111",
     "description": "High-accuracy web search across many sources.",
     "words": ["parallel search", "parallel.ai"], "words_only": True},
    {"id": "jina", "logo": "jina.png", "title": "Jina Reader", "url": "https://mcp.jina.ai/v1", "auth": "none",
     "category": "Research", "color": "#009191",
     "description": "Read any web page, search the web and arXiv, take screenshots.",
     "words": ["jina", "read this page", "read the page", "arxiv"], "words_only": True},
    {"id": "firecrawl", "logo": "firecrawl.ico", "title": "Firecrawl", "url": "https://mcp.firecrawl.dev/mcp", "auth": "none",
     "category": "Research", "color": "#ff4c00",
     "description": "Scrape and search the web into clean text.", "words": ["firecrawl", "scrape this"]},
    {"id": "kiwi", "logo": "kiwi.png", "title": "Kiwi.com Flights", "url": "https://mcp.kiwi.com", "auth": "none",
     "category": "Research", "color": "#00a991",
     "description": "Search real flights and prices between cities.",
     "words": ["flight", "flights", "fly to", "plane ticket", "kiwi.com"], "words_only": True},
]
APPS_BY_ID = {a["id"]: a for a in APPS}


def app_client(app_id):
    """(client_id, secret) the owner registered for an "app"-auth entry."""
    key = re.sub(r"[^A-Z0-9]", "_", app_id.upper())
    cid = os.environ.get("MCP_%s_CLIENT_ID" % key, "").strip()
    sec = os.environ.get("MCP_%s_CLIENT_SECRET" % key, "").strip()
    return (cid, sec) if cid else (None, None)


def available_apps():
    """The gallery, minus "app"-auth entries the owner has not set up."""
    return [a for a in APPS if a["auth"] != "allowlist"
            and (a["auth"] != "app" or app_client(a["id"])[0])]


def app_mentioned(app, text):
    t = " %s " % (text or "").lower()
    if "@" + app["id"] in t:
        return True
    words = ([] if app.get("words_only") else [app["title"].lower()]) + [
        w.lower() for w in app.get("words") or []]
    return any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w.strip()), t)
               for w in words if w.strip())


# ----------------------------------------------------------------------
# SKILLS
#
# id:           what /id calls it by
# triggers:     a regex over the message; a match switches the skill on
#               for that reply. None means "only by name".
# handled_by:   shown in the list but done by a dedicated part of the app
#               (the website builder, the diagram renderer) rather than by
#               adding instructions.
# ----------------------------------------------------------------------
def _rx(words):
    return r"\b(?:%s)\b" % words


BUILTIN_SKILLS = [
    {"id": "website", "title": "Website builder", "icon": "🌐",
     "description": "Plans, writes and reviews a complete multi-file website you can publish.",
     "handled_by": "builder", "triggers": None, "instructions": ""},
    {"id": "diagrams", "title": "Diagrams", "icon": "🔀",
     "description": "Flowcharts, sequence diagrams and mind maps, drawn in the chat.",
     "handled_by": "diagram", "triggers": None, "instructions": ""},
    {"id": "slides", "title": "Slide decks", "icon": "📊",
     "description": "A presentation as one HTML file with keyboard navigation.",
     "triggers": _rx(r"slides?|slide ?deck|presentation|pitch deck|keynote|powerpoint|pptx"),
     "instructions": """Make the deck as ONE self-contained HTML file in a single ```html block named slides.html.
- Plan first: one line per slide (8-14 slides unless asked otherwise). One idea per slide; a headline that states the point, not a topic label.
- Each slide is a <section class="slide"> filling the viewport (16:9 safe area, 100vh). Arrow keys, space and click advance; Home/End jump; show "n / total" in a corner. Use a CSS variable palette with one accent colour, a clean sans-serif, generous whitespace, and large type (headline 44-60px, body 22-28px).
- At most 5 bullets per slide and about 12 words per bullet. Prefer a big number, a short quote, a simple chart drawn with CSS/SVG, or a two-column comparison over walls of text.
- Include a title slide and a closing slide with the one thing to remember. Speaker notes go in a hidden <aside class="notes"> per slide; pressing N toggles them.
- Add print CSS so each slide prints on its own landscape page (for PDF export).
After the code, tell the person in one sentence how to present it and export to PDF."""},
    {"id": "documents", "title": "Documents & reports", "icon": "📄",
     "description": "Reports, proposals, letters and briefs that are ready to send.",
     "triggers": _rx(r"report|proposal|business plan|white ?paper|memo|cover letter|letter|brief|essay|policy|contract draft|terms of service|privacy policy"),
     "instructions": """Write a finished document, not an outline of one.
- Start with a title and a 2-3 sentence summary of the conclusion or ask. Then sections with clear headings, short paragraphs, and tables where comparing things.
- Match the register to the document: formal for letters, contracts and policies; plain and direct for reports and memos. No filler, no "In today's fast-paced world".
- Put placeholders for facts you do not know in [square brackets] rather than inventing names, figures or dates, and list them at the end under "To fill in".
- For legal-style documents, add one line saying it is a starting draft to be checked by a qualified professional.
- End with a one-line offer to export it (Markdown or a printable HTML page in a ```html block) if that would help."""},
    {"id": "spreadsheets", "title": "Spreadsheets", "icon": "📈",
     "description": "Tables, budgets and trackers with working formulas, ready for Excel or Sheets.",
     "triggers": _rx(r"spreadsheet|excel|google sheets?|csv|budget|tracker|formula|vlookup|xlookup|pivot|invoice table"),
     "instructions": """Build the sheet so it can be pasted straight into Excel or Google Sheets.
- Give it as a ```csv block (comma-separated, header row first, values quoted when they contain commas). Put formulas in cells exactly as typed in Sheets/Excel (e.g. =SUM(B2:B13)), referencing real cell addresses for the layout you produced.
- Also show the same table as a Markdown table so it is readable in the chat, with a column-letter row if formulas are involved.
- Inputs first, calculations after, totals at the bottom; one row per item, one column per attribute; no merged cells.
- Then list each formula with one line on what it does, and any assumption you made (currency, tax rate, dates).
- For anything that needs real computation over data the person gave you, use the run_python tool to compute the numbers rather than estimating them."""},
    {"id": "data-analysis", "title": "Data analysis", "icon": "🧮",
     "description": "Cleans data, computes statistics and draws charts by running Python.",
     "triggers": _rx(r"analy[sz]e|analysis|statistics?|average|median|correlation|regression|chart|graph|plot|dataset|trend"),
     "instructions": """Answer from computed results, not guesses.
- When there is data (pasted, attached, or described exactly), use the run_python tool: load it, check shape, missing values and types first, then compute. Print the numbers you will quote.
- Draw a chart when a comparison or trend is the point: matplotlib, one clear message per chart, labelled axes with units, a title that states the finding.
- Report: the answer in one sentence first, then the key numbers in a small table, then caveats (sample size, outliers, what correlation does not show).
- If no tool is available, show the exact code the person can run and say the numbers are not yet computed."""},
    {"id": "email", "title": "Emails", "icon": "✉️",
     "description": "Clear emails with the right tone - replies, follow-ups, cold outreach.",
     "triggers": _rx(r"e-?mail|reply to (?:this|him|her|them)|follow[- ]up|outreach|newsletter"),
     "instructions": """Write the email ready to send.
- Give a subject line, then the body. Lead with the purpose in the first sentence; one request per email; say exactly what you need and by when.
- Keep it short: 50-150 words unless it is a newsletter. Short paragraphs, no "I hope this email finds you well".
- Match the tone asked for (or the thread's tone if one was pasted). Sign off with the person's name if known, else [Your name].
- Offer one alternative version (shorter, or warmer/firmer) only if the tone is a judgement call."""},
    {"id": "code-review", "title": "Code review", "icon": "🔍",
     "description": "Finds bugs, security issues and simplifications in code you paste.",
     "triggers": _rx(r"review (?:this|my)|code review|is this code|what'?s wrong with (?:this|my) code|refactor"),
     "instructions": """Review like a careful senior engineer.
- First the verdict in one line. Then findings ordered by severity: bugs and security problems first (with the exact line and a concrete input that breaks it), then correctness risks, then simplifications. Skip style nits unless asked.
- For each finding: what is wrong, why it matters, and the fixed code.
- Check: input validation, error handling, off-by-one and empty cases, concurrency, injection (SQL, shell, HTML), secrets in code, resource leaks, and performance on large inputs.
- Say plainly if the code is fine. Do not invent problems to have something to say."""},
    {"id": "debugging", "title": "Debugging", "icon": "🐞",
     "description": "Traces an error to its cause and fixes it.",
     "triggers": _rx(r"error|exception|traceback|stack trace|bug|not working|doesn'?t work|crash(?:es|ing)?|fails?|undefined is not"),
     "instructions": """Debug methodically.
- Read the error and name the most likely cause first, pointing at the line it comes from. If the cause is ambiguous, list the top 2-3 hypotheses and the quickest check that tells them apart.
- Give the fix as a minimal change (the corrected lines, not a rewrite), and explain in one or two sentences why it fixes the cause rather than the symptom.
- If information is missing (the code that raised, versions, the full traceback), say exactly what to paste.
- When the fix can be verified by running code, use the run_python tool to reproduce and confirm."""},
    {"id": "resume", "title": "Résumés & cover letters", "icon": "🧾",
     "description": "Achievement-focused CVs and tailored cover letters.",
     "triggers": _rx(r"r[ée]sum[ée]|\bcv\b|curriculum vitae|cover letter|linkedin (?:profile|summary)|job application"),
     "instructions": """Write for a recruiter skimming for 10 seconds.
- Bullets start with a strong verb and show results with numbers (grew, cut, shipped, led - by how much, for whom). One to two lines each.
- Order: contact line, 2-3 line summary aimed at the target role, experience (reverse chronological), skills, education. One page unless 10+ years of experience.
- Tailor to the job description if one is given: mirror its key terms honestly.
- Never invent jobs, degrees or numbers; use [placeholders] and list what to fill in.
- Cover letters: 250-350 words, a specific reason for this company, two proofs of fit, a clear close."""},
    {"id": "tutor", "title": "Study tutor", "icon": "🎓",
     "description": "Explains step by step and checks understanding.",
     "triggers": _rx(r"explain|teach me|help me understand|homework|exercise|exam|how does .* work|step by step"),
     "instructions": """Teach, do not just answer.
- Start from what the person likely knows; define new terms the first time.
- Work examples step by step, showing why each step follows. For maths, write each line of working; check the final answer by substituting back or estimating.
- End with a one-line summary and one short practice question (with the answer hidden under a "Answer" line at the end) when it is a learning request."""},
    {"id": "translate", "title": "Translation", "icon": "🌍",
     "description": "Faithful, natural translation that keeps tone and formatting.",
     "triggers": _rx(r"translate|translation|in (?:french|arabic|spanish|german|english|italian|portuguese|chinese|japanese|turkish|dutch)"),
     "instructions": """Translate faithfully and naturally.
- Keep meaning, tone and formatting (lists, Markdown, line breaks). Do not summarise or add content.
- Prefer natural phrasing over word-for-word; keep names, code and URLs unchanged.
- If a phrase is ambiguous or an idiom has no equivalent, translate it and add a short note after the translation."""},
]

# Skills that come with plugins. Same shape; active only while the
# plugin is installed.
PLUGIN_SKILLS = {
    "write-tests": {"title": "Write tests", "icon": "✅",
                    "description": "Unit tests that cover the edge cases, in the project's framework.",
                    "triggers": _rx(r"write tests?|unit tests?|test cases|pytest|jest|coverage"),
                    "instructions": """Write tests that would catch real bugs.
- Use the project's framework (pytest, Jest, Vitest, JUnit...) - ask only if it cannot be inferred.
- Cover: the normal case, boundaries (empty, one, many, max), invalid input, and each error path. One behaviour per test, named after what it checks.
- No tests that only re-state the implementation. Mock only external services, not the code under test.
- Finish with the command to run them."""},
    "release-notes": {"title": "Release notes", "icon": "📝",
                      "description": "Changelogs and release notes from commits or a diff.",
                      "triggers": _rx(r"release notes?|changelog|what changed|patch notes"),
                      "instructions": """Turn changes into release notes users understand.
- Group under New, Improved, Fixed (and Breaking, first, if any). One line per change, written as the user-visible effect, not the code change.
- Drop internal refactors unless they change behaviour. Include upgrade steps for breaking changes."""},
    "meeting-notes": {"title": "Meeting notes", "icon": "🗒️",
                      "description": "Turns a transcript or rough notes into decisions and action items.",
                      "triggers": _rx(r"meeting notes|minutes|transcript|action items|standup notes"),
                      "instructions": """Summarise a meeting so someone who missed it can act.
- Sections: Summary (3 lines), Decisions, Action items (owner - task - due date), Open questions.
- Use names exactly as given; mark unknown owners or dates as [unassigned]/[no date]. Do not invent decisions."""},
    "project-plan": {"title": "Project plans", "icon": "🗺️",
                     "description": "Milestones, tasks, owners and risks for a project.",
                     "triggers": _rx(r"project plan|roadmap|milestones?|timeline|sprint plan|gantt"),
                     "instructions": """Make a plan someone could start on tomorrow.
- Goal and definition of done first. Then milestones with dates, each broken into tasks small enough for one person in a few days, with dependencies.
- A risks table (risk, likelihood, impact, mitigation). Present the schedule as a Markdown table; offer a CSV for import into a tracker."""},
    "weekly-update": {"title": "Status updates", "icon": "📣",
                      "description": "Weekly updates and status reports for a team or client.",
                      "triggers": _rx(r"weekly update|status report|status update|progress report"),
                      "instructions": """Write a status update that can be read in 30 seconds.
- Overall status (on track / at risk / blocked) in the first line with the reason. Then Done, Next, Blockers / help needed. Numbers over adjectives."""},
    "seo-blog": {"title": "SEO articles", "icon": "🔎",
                 "description": "Search-optimised blog posts with title, meta description and structure.",
                 "triggers": _rx(r"blog post|seo|article about|keyword"),
                 "instructions": """Write an article that ranks and gets read.
- Give: title (under 60 characters, keyword near the front), meta description (under 155 characters), URL slug.
- Answer the search intent in the first paragraph. H2/H3 structure that mirrors the questions people ask; short paragraphs; a table or list where it helps; an FAQ section of 3-5 questions at the end.
- Use the keyword and natural variants without stuffing. Suggest 2-3 internal link anchors in [brackets]."""},
    "social-media": {"title": "Social posts", "icon": "📱",
                     "description": "Posts tailored to each platform, with hooks and hashtags.",
                     "triggers": _rx(r"instagram|tiktok|linkedin post|tweet|twitter|x post|facebook post|caption|social media"),
                     "instructions": """Write posts native to each platform.
- A hook in the first line. LinkedIn: 80-200 words, line breaks, one takeaway. X: under 280 characters, or a numbered thread. Instagram/TikTok: caption plus a short on-screen text idea.
- 3-5 relevant hashtags where the platform uses them. A clear call to action. Offer two variants with different hooks."""},
    "email-campaign": {"title": "Email campaigns", "icon": "📬",
                       "description": "Sequences and newsletters with subject lines that get opened.",
                       "triggers": _rx(r"email campaign|email sequence|drip|newsletter|onboarding emails?"),
                       "instructions": """Plan and write the campaign.
- For a sequence: a table of emails (day, goal, subject), then each email in full. Each has one goal and one call to action.
- Two subject-line options per email (under 50 characters) plus preview text. Plain, personal tone; no spammy words in subjects."""},
    "brand-voice": {"title": "Brand voice", "icon": "🎙️",
                    "description": "Defines and applies a consistent voice for a brand.",
                    "triggers": _rx(r"brand voice|tone of voice|brand guidelines|tagline|slogan"),
                    "instructions": """Make the voice concrete.
- Describe it as 3-4 traits, each with "we are / we are not" and a before/after example sentence.
- Give a short word list (use / avoid). When asked for copy, apply the voice and note which traits it shows."""},
    "invoice": {"title": "Invoices & quotes", "icon": "🧾",
                "description": "Professional invoices and quotes with correct totals.",
                "triggers": _rx(r"invoice|quote|quotation|estimate for (?:a )?client|bill (?:a|the) client"),
                "instructions": """Produce an invoice or quote with arithmetic that adds up.
- Header (from, to, number, date, due date), line items (description, qty, unit price, amount), subtotal, tax with rate, total, payment terms and methods.
- Compute every amount exactly (use run_python when there are many lines). Unknown details go in [brackets].
- Offer it as a printable HTML page in one ```html block."""},
    "research-report": {"title": "Research reports", "icon": "🔬",
                        "description": "Sourced, structured research with citations.",
                        "triggers": _rx(r"research|literature|sources|state of the art|market research|compare (?:the )?(?:options|tools)"),
                        "instructions": """Research before writing, and cite.
- Search the web (web_search, or a connected search app) for current sources; prefer primary sources and recent dates.
- Structure: key findings first (bullets), then sections, then a comparison table if there are options, then open questions.
- Cite every factual claim with a link. Say where sources disagree or evidence is thin."""},
    "design-critique": {"title": "Design critique", "icon": "🎨",
                        "description": "Actionable feedback on layouts, screens and visuals.",
                        "triggers": _rx(r"design (?:feedback|critique|review)|critique (?:this|my)|ui review|ux review"),
                        "instructions": """Critique against principles, with fixes.
- Check hierarchy, spacing and alignment, contrast and accessibility (WCAG AA), consistency, and whether the main action is obvious.
- Order findings by impact. Each: what you see, why it hurts, the specific change (sizes, colours, copy)."""},
    "ux-copy": {"title": "UX writing", "icon": "✍️",
                "description": "Button labels, errors, empty states and onboarding copy.",
                "triggers": _rx(r"ux copy|microcopy|button label|error message|empty state|onboarding copy"),
                "instructions": """Write interface text people do not have to read twice.
- Labels say what happens ("Save changes", not "OK"). Errors say what went wrong and how to fix it, without blame. Sentence case, no jargon.
- Give 2-3 options for anything visible and important, with the one you recommend first."""},
}

# ----------------------------------------------------------------------
# PLUGINS
# ----------------------------------------------------------------------
PLUGINS = [
    {"id": "developer", "title": "Developer", "icon": "💻",
     "description": "Code review, debugging and tests, plus GitLab, Supabase, Netlify, Sentry and live docs.",
     "skills": ["code-review", "debugging", "write-tests", "release-notes"],
     "apps": ["github", "gitlab", "supabase", "netlify", "sentry", "context7", "awsdocs", "deepwiki"]},
    {"id": "productivity", "title": "Productivity", "icon": "⚡",
     "description": "Meeting notes, plans and status updates, connected to Notion, Linear, ClickUp, Todoist and Granola.",
     "skills": ["meeting-notes", "project-plan", "weekly-update", "documents", "email"],
     "apps": ["notion", "linear", "atlassian", "clickup", "todoist", "airtable", "granola", "zapier"]},
    {"id": "marketing", "title": "Marketing", "icon": "📣",
     "description": "SEO articles, social posts, email campaigns and brand voice, with Mailchimp, Klaviyo and Webflow.",
     "skills": ["seo-blog", "social-media", "email-campaign", "brand-voice"],
     "apps": ["mailchimp", "klaviyo", "apollo", "webflow", "wix", "hubspot"]},
    {"id": "commerce", "title": "Commerce", "icon": "🛒",
     "description": "Invoices and quotes with exact totals, connected to Stripe, PayPal, Ramp and Mercury.",
     "skills": ["invoice", "spreadsheets"],
     "apps": ["stripe", "paypal", "ramp", "mercury"]},
    {"id": "research", "title": "Research", "icon": "🔬",
     "description": "Sourced research reports and data analysis, with Exa, Tavily, Jina Reader and Hugging Face.",
     "skills": ["research-report", "data-analysis"],
     "apps": ["exa", "tavily", "jina", "firecrawl", "huggingface"]},
    {"id": "design", "title": "Design", "icon": "🎨",
     "description": "Design critique, UX writing and slide decks.",
     "skills": ["design-critique", "ux-copy", "slides"],
     "apps": ["miro", "cloudinary"]},
]
PLUGINS_BY_ID = {p["id"]: p for p in PLUGINS}

BUILTIN_BY_ID = {s["id"]: s for s in BUILTIN_SKILLS}

# What a user skill may be called: /lowercase-words, not shadowing a
# built-in or plugin skill.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
MAX_USER_INSTRUCTIONS = 8000
MAX_AUTO_SKILLS = 2


def reserved_slug(slug):
    return slug in BUILTIN_BY_ID or slug in PLUGIN_SKILLS


def plugin_skill(skill_id, plugin_id):
    s = PLUGIN_SKILLS.get(skill_id)
    if not s:
        return None
    return dict(s, id=skill_id, source="plugin:" + plugin_id)


def active_skills(user_skills, installed_plugins):
    """Every skill this person has on: built-ins, their own (enabled),
    and those of the plugins they installed."""
    out, seen = [], set()
    for s in BUILTIN_SKILLS:
        out.append(dict(s, source="builtin"))
        seen.add(s["id"])
    for pid in installed_plugins:
        p = PLUGINS_BY_ID.get(pid)
        for sid in (p or {}).get("skills", []):
            if sid in seen:
                continue
            s = plugin_skill(sid, pid)
            if s:
                out.append(s)
                seen.add(sid)
    for s in user_skills:
        if s.get("enabled") and s["slug"] not in seen:
            words = [w.strip() for w in (s.get("triggers") or "").split(",") if w.strip()]
            out.append({"id": s["slug"], "title": s["title"], "icon": "✨",
                        "description": s.get("description") or "",
                        "instructions": s.get("instructions") or "",
                        "triggers": (r"(?<![a-z0-9])(?:%s)(?![a-z0-9])"
                                     % "|".join(re.escape(w.lower()) for w in words)) if words else None,
                        "source": "user"})
            seen.add(s["slug"])
    return out


_SLASH = re.compile(r"(?:^|\s)/([a-z0-9][a-z0-9-]{1,31})\b")


def match_skills(text, skills, exclude=()):
    """-> the skills to use for this message, named ones first.

    /name anywhere in the message picks that skill whatever else it says.
    Otherwise up to MAX_AUTO_SKILLS whose triggers match switch on.
    Skills handled elsewhere (website, diagrams) never add instructions.
    """
    text = text or ""
    low = text.lower()
    by_id = {s["id"]: s for s in skills}
    picked = []
    for name in _SLASH.findall(low):
        s = by_id.get(name)
        if s and s not in picked and not s.get("handled_by") and s["id"] not in exclude:
            picked.append(s)
    if picked:
        return picked
    for s in skills:
        if len(picked) >= MAX_AUTO_SKILLS:
            break
        if s.get("handled_by") or not s.get("triggers") or s["id"] in exclude:
            continue
        try:
            if re.search(s["triggers"], low):
                picked.append(s)
        except re.error:
            continue
    return picked


def skill_block(skill):
    who = ("written by the person you are talking to - follow it unless it "
           "conflicts with the rules above" if skill.get("source") == "user"
           else "part of RandomGenerals")
    return ("## Skill: %s\nThis reply uses the \"%s\" skill (%s). Apply these "
            "instructions to this reply:\n\n%s" % (
                skill["title"], skill["id"], who,
                (skill.get("instructions") or "")[:MAX_USER_INSTRUCTIONS]))
