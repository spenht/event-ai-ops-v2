# 2clicks.com — Master Context File
**Last updated: 2026-03-24 (Session: AI calls + Agent Profiles + Financial Dashboard)**

## VISION
Build the #1 SaaS for client acquisition in LATAM → $1B company.
Spencer has 5M social media followers, 40K+ course buyers, thousands of 2clicks users.
Currently white-labeling GoHighLevel, building own platform to replace it.

## WHAT'S LIVE & WORKING
- ✅ AI Voice Calls (ElevenLabs clone of Spencer's voice + OpenAI Realtime)
- ✅ Call Center (WebRTC, Telnyx, spartan + AI agents)
- ✅ WhatsApp Automation (Twilio, chatbot, follow-ups, ticket delivery)
- ✅ Landing Page Builder (Claude Opus AI, templates, visual editor)
- ✅ Ticket/Boleto system (QR codes, WhatsApp + SMS delivery)
- ✅ Lead capture + Meta CAPI + UTM tracking
- ✅ Agent Profiles (Confirmador, Setter, Closer, Seguimiento, Upsell, Líder)
- ✅ Commission Engine (auto-attribution, volume escalation, profile-based)
- ✅ 4 Stripe accounts connected (UVUL MX, LBA, OLL, 2CLICKS)
- ✅ 3 Mercury accounts connected (OLL, 2CLICKS, LBA)
- ✅ 1 Whop account connected
- ✅ Recording for all calls
- ✅ Post-call AI analysis (GPT-4o-mini)
- ✅ 3 Fly.io servers scaled

## ACCOUNTS & KEYS (stored in Fly secrets)
- Stripe: STRIPE_KEY_UVUL, STRIPE_KEY_LBA, STRIPE_KEY_OLL, STRIPE_KEY_2CLICKS
- Mercury: MERCURY_KEY_OLL, MERCURY_KEY_2CLICKS, MERCURY_KEY_LBA
- Whop: WHOP_API_KEY
- Telnyx: TELNYX_API_KEY, TELNYX_SIP_CONNECTION_ID
- ElevenLabs: ELEVENLABS_API_KEY (voice: BbbtvZxiMbR5KJkRsqcC "Spencer 1")
- Anthropic: ANTHROPIC_API_KEY (Claude Opus for landing pages)
- OpenAI: OPENAI_API_KEY (Realtime API for voice calls)
- Twilio: ACcfbfaa84e1a092be65596efbab6af33a (WhatsApp + SMS)
- Toll-free SMS: +18885564279 (verification: IN_REVIEW, SID: HH7b5e77e6392b4bf6560520b7659b17db)

## DEPLOY WORKFLOW
- Backend: `cd event-ai-ops-v2 && fly deploy -a calls-mx --remote-only`
- Dashboard: `cd event-ai-ops-saas/dashboard && npx vercel --prod`
- Landing: `cd event-ai-ops-saas/landing && git push origin main`

## ENTERPRISES
1. UNA VIDA UN LEGADO SA DE CV (Mexico) - Stripe MXN
2. LEGACY BUSINESS ACADEMY LLC (USA, EIN: 38-4335009) - Stripe USD + Mercury
3. ONE LIFE LEGACY LLC (USA) - Stripe USD + Mercury
4. 2CLICKS.COM LLC - Stripe USD + Mercury

## ACTIVE CAMPAIGNS
- Beyond Wealth Miami (e4809b3b): 27-29 March 2026, EB Hotel Miami, 6527 leads
- Beyond Wealth Miami 2 (c0000000): same event, 4965 leads
- Beyond Wealth Costa Rica (24bca326): past event
- Cashflow Master México (dfbdeecc): past event

## TEAM
- Spencer Hoffmann (Owner) - spenht@gmail.com
- Diego (Developer) - diegob.cn2024@gmail.com / GitHub: tech-uvul
- Marcelo - marcelo@onelifelegacy.com (agent/admin)
- 6+ spartans across campaigns

## AGENT PROFILES SYSTEM
- confirmador: calls leads to confirm event attendance
- setter: books appointments for closers
- closer: closes sales via Zoom/phone
- seguimiento: follows up warm leads post-event
- upsell: sells premium offers to existing buyers
- lider: manages project, earns % of project profit

## COMMISSION RULES (per Spencer)
- Confirmador: 3.5% of sales from leads they called, or 10%+ of VIP tickets
- Setter: small commission per closed sale from their appointments
- Closer: % of the sale amount they close
- Seguimiento: 3.5% hot leads, 5% cold/self-generated leads
- Upsell: similar to seguimiento but higher ticket
- Líder: commission on own sales + 5-10% of PROJECT PROFIT

## WHAT'S BEING BUILT NEXT
1. **Business Intelligence Dashboard** (Spencer's executive view)
   - All 4 Stripe accounts + 3 Mercury accounts + Whop in one view
   - Revenue by day/week/month per project
   - Costs tracking (Mercury transactions linked to projects)
   - Profitability per project/agent/campaign
   - Agent performance metrics

2. **Commission Configuration UI** — visual rules editor per profile per campaign

3. **SMS Blast System** — toll-free verification pending, endpoint + auto-response built

4. **Stripe Connect Express** — auto-pay spartans in their local currency

5. **Website Builder Improvements** — better Claude prompts, inline editor, cloner

## KNOWN ISSUES
- ElevenLabs WebSocket unstable (1006/1008 disconnects randomly)
- AI calls: ~60% connect, voicemail detection needs improvement
- Toll-free SMS verification still IN_REVIEW
- Landing page templates need verification after deploy
- Telnyx 10DLC not registered (needed for local number SMS)

## KEY FILES
- Backend: /Users/spencerhoffmann/dev/event-ai-ops-v2/app/
- Dashboard: /Users/spencerhoffmann/dev/event-ai-ops-saas/dashboard/
- AI Voice: app/services/ai_voice.py (1379 lines)
- Call Media WS: app/routes/call_media_ws.py (790 lines)
- Commission Engine: app/services/commission_engine.py
- Landing Pages: app/routes/landing_pages.py
- Agent Profiles: app/routes/agent_profiles.py
- SMS Incoming: app/routes/sms_incoming.py
