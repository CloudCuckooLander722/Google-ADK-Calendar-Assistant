import streamlit as st

PREMISE = """
WorkFlow is an AI scheduling workspace that helps people plan their calendars, protect attention, and turn scattered commitments into a calmer rhythm.
"""

# st.html (not st.markdown) so the markup isn't parsed as Markdown: there,
# indented lines and blank lines inside HTML turn into code blocks.
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,600;0,700;1,600&display=swap');

/* The feature chips are #links; Streamlit scrolls the page inside its own
   containers, so smooth scrolling is set on those as well as the root. */
html, [data-testid="stMain"], [data-testid="stAppViewContainer"] { scroll-behavior: smooth; }

.wf-home {
    --ink: #14213d;
    --muted: #56616f;
    --accent: #005a4b;
    --mint: #7edec3;
    --violet: #7a77ff;
    --card: rgba(255, 255, 255, 0.72);
    --line: rgba(20, 33, 61, 0.12);
    color: var(--ink);
    font-family: 'Montserrat', 'Open Sans', sans-serif;
    background:
        radial-gradient(circle at 72% 8%, rgba(126, 222, 195, 0.16), transparent 22%),
        radial-gradient(circle at 12% 60%, rgba(122, 119, 255, 0.10), transparent 24%),
        linear-gradient(180deg, #eef7f6 0%, #eef3f8 100%);
    border-radius: 28px;
    overflow: hidden;
}
.wf-home a { color: inherit; text-decoration: none; }
.wf-home section { scroll-margin-top: 72px; }

/* Navigation header */
.wf-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    padding: 22px 48px;
    border-bottom: 1px solid var(--line);
}
.wf-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: 'Playfair Display', Georgia, serif;
    font-size: 24px;
    font-weight: 700;
}
.wf-brand span {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    background: var(--ink);
    color: #eefcf8;
    font-family: 'Montserrat', sans-serif;
    font-size: 15px;
    font-weight: 800;
}
.wf-home .wf-nav ul {
    list-style: none;
    padding-inline-start: 0;
    display: flex;
    gap: 28px;
    margin: 0;
    padding: 0;
    align-items: center;
}
.wf-nav li { margin: 0; font-size: 14px; font-weight: 600; color: var(--muted); }
.wf-nav li a:hover { color: var(--ink); }
.wf-nav .wf-nav-cta a {
    padding: 10px 18px;
    border-radius: 999px;
    background: var(--ink);
    color: #eefcf8;
}
.wf-nav .wf-nav-cta a:hover { background: #223b59; color: #eefcf8; }

/* Hero + feature list header */
.wf-hero { max-width: 980px; margin: 0 auto; padding: 88px 48px 64px; }
.wf-eyebrow {
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.16em;
    color: var(--accent);
    text-transform: uppercase;
    margin-bottom: 14px;
}
.wf-hero h1 {
    font-family: 'Playfair Display', Georgia, serif;
    font-size: clamp(48px, 6vw, 84px);
    line-height: 1;
    letter-spacing: -0.03em;
    margin: 0 0 24px;
    padding: 0;
    color: var(--ink);
}
.wf-hero h1 em { color: var(--accent); }
.wf-hero p { font-size: 19px; line-height: 1.75; color: var(--muted); max-width: 720px; margin: 0 0 32px; }
/* Streamlit indents lists; the chips line up with the text above instead. */
.wf-home .wf-features { list-style: none; display: flex; flex-wrap: wrap; gap: 12px; margin: 0; padding: 0; padding-inline-start: 0; }
.wf-features li { margin: 0; }
.wf-features a {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 11px 18px;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: var(--card);
    font-size: 13px;
    font-weight: 700;
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
}
.wf-features a:hover {
    transform: translateY(-2px);
    border-color: rgba(0, 90, 75, 0.35);
    box-shadow: 0 10px 24px rgba(20, 33, 61, 0.10);
}
.wf-features a::after { content: "↓"; color: var(--accent); }

/* Demo sections */
.wf-demo {
    max-width: 1080px;
    margin: 0 auto;
    padding: 64px 48px;
    display: grid;
    grid-template-columns: 0.9fr 1.1fr;
    gap: 56px;
    align-items: center;
    border-top: 1px solid var(--line);
}
.wf-demo h2 {
    font-family: 'Playfair Display', Georgia, serif;
    font-size: 40px;
    line-height: 1.1;
    margin: 0 0 16px;
    padding: 0;
    color: var(--ink);
}
.wf-demo p { font-size: 16px; line-height: 1.75; color: var(--muted); margin: 0; }
.wf-stage {
    position: relative;
    background: var(--card);
    border: 1px solid rgba(255, 255, 255, 0.95);
    border-radius: 24px;
    box-shadow: 0 30px 80px rgba(20, 33, 61, 0.14);
    padding: 22px;
    min-height: 300px;
    overflow: hidden;
}

/* Shared chat pieces. Every demo runs on one 9s loop; each piece's keyframes
   say when in that loop it appears, so the steps play in order. */
.wf-bubble {
    max-width: 82%;
    padding: 11px 15px;
    border-radius: 16px;
    font-size: 13px;
    line-height: 1.5;
    margin-bottom: 10px;
    opacity: 0;
}
.wf-user { margin-left: auto; background: var(--ink); color: #eefcf8; border-bottom-right-radius: 4px; }
.wf-bot { background: #ffffff; border: 1px solid var(--line); border-bottom-left-radius: 4px; }
/* The typing dots sit where the reply will appear, then fade as it arrives. */
.wf-slot { position: relative; }
.wf-slot .wf-typing { position: absolute; top: 0; left: 0; }
.wf-typing { display: flex; gap: 4px; padding: 13px 15px; width: 52px; }
.wf-typing i { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); animation: wf-dot 0.9s infinite; }
.wf-typing i:nth-child(2) { animation-delay: 0.15s; }
.wf-typing i:nth-child(3) { animation-delay: 0.3s; }

.wf-step1 { animation: wf-step1 9s infinite; }
.wf-step2 { animation: wf-step2 9s infinite; }
.wf-step3 { animation: wf-step3 9s infinite; }
.wf-step4 { animation: wf-step4 9s infinite; }
.wf-step5 { animation: wf-step5 9s infinite; }
.wf-step6 { animation: wf-step6 9s infinite; }

/* Scheduling: a request, then the event drops onto the week. */
.wf-week {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 6px;
    margin-top: 14px;
}
.wf-day { font-size: 11px; font-weight: 700; color: var(--muted); text-align: center; }
.wf-col { position: relative; height: 120px; border-radius: 10px; background: rgba(20, 33, 61, 0.04); }
.wf-busy { position: absolute; left: 4px; right: 4px; border-radius: 6px; background: rgba(122, 119, 255, 0.25); }
.wf-event {
    position: absolute;
    left: 4px;
    right: 4px;
    top: 52px;
    height: 44px;
    border-radius: 6px;
    background: var(--accent);
    color: #eefcf8;
    font-size: 10px;
    font-weight: 700;
    padding: 5px;
    opacity: 0;
    animation: wf-drop 9s infinite;
}

/* Documents: a file goes in, a progress bar fills, deadlines come out. */
.wf-file {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-radius: 12px;
    background: #ffffff;
    border: 1px solid var(--line);
    font-size: 13px;
    font-weight: 700;
    opacity: 0;
    animation: wf-slide-in 9s infinite;
}
.wf-file b { padding: 3px 6px; border-radius: 6px; background: #e8453c; color: #fff; font-size: 10px; }
.wf-progress { height: 6px; border-radius: 999px; background: rgba(20, 33, 61, 0.08); margin: 14px 0 18px; overflow: hidden; }
.wf-progress span { display: block; height: 100%; width: 0; background: linear-gradient(90deg, var(--mint), var(--accent)); animation: wf-fill 9s infinite; }
.wf-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    border-radius: 12px;
    background: #ffffff;
    border: 1px solid var(--line);
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 8px;
    opacity: 0;
}
.wf-row small { color: var(--accent); font-weight: 800; }

/* Try header */
.wf-try { text-align: center; padding: 88px 48px 96px; border-top: 1px solid var(--line); }
.wf-try h2 {
    font-family: 'Playfair Display', Georgia, serif;
    font-size: clamp(36px, 4.5vw, 56px);
    margin: 0 0 14px;
    padding: 0;
    color: var(--ink);
}
.wf-try p { color: var(--muted); font-size: 17px; margin: 0 0 30px; }
.wf-try a {
    display: inline-block;
    padding: 16px 30px;
    border-radius: 14px;
    background: var(--ink);
    color: #eefcf8;
    font-weight: 800;
    letter-spacing: 0.06em;
    font-size: 14px;
    transition: background 0.2s ease, transform 0.2s ease;
}
.wf-try a:hover { background: #223b59; transform: translateY(-2px); }
.wf-footer { padding: 18px 48px; font-size: 12px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); border-top: 1px solid var(--line); }

@keyframes wf-dot { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-4px); opacity: 1; } }
@keyframes wf-step1 { 0%, 4% { opacity: 0; transform: translateY(8px); } 9%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-step2 { 0%, 14% { opacity: 0; } 18%, 34% { opacity: 1; } 37%, 100% { opacity: 0; } }
@keyframes wf-step3 { 0%, 36% { opacity: 0; transform: translateY(8px); } 41%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-step4 { 0%, 46% { opacity: 0; transform: translateX(-10px); } 51%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-step5 { 0%, 54% { opacity: 0; transform: translateX(-10px); } 59%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-step6 { 0%, 62% { opacity: 0; transform: translateX(-10px); } 67%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-drop { 0%, 44% { opacity: 0; transform: translateY(-24px) scale(0.9); } 52%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-slide-in { 0%, 3% { opacity: 0; transform: translateX(-24px); } 10%, 92% { opacity: 1; transform: none; } 100% { opacity: 0; } }
@keyframes wf-fill { 0%, 12% { width: 0; } 38%, 92% { width: 100%; } 100% { width: 100%; opacity: 0; } }

@media (max-width: 760px) {
    .wf-nav, .wf-hero, .wf-demo, .wf-try, .wf-footer { padding-left: 20px; padding-right: 20px; }
    .wf-demo { grid-template-columns: 1fr; gap: 28px; }
    .wf-nav ul { gap: 16px; }
}

/* For people who turn animations off, show each demo's finished state. */
@media (prefers-reduced-motion: reduce) {
    html, [data-testid="stMain"], [data-testid="stAppViewContainer"] { scroll-behavior: auto; }
    .wf-home *, .wf-home *::after { animation: none !important; transition: none !important; }
    .wf-bubble, .wf-event, .wf-file, .wf-row { opacity: 1; }
    .wf-typing { display: none; }
    .wf-progress span { width: 100%; }
}
</style>
"""

PAGE = """
<div class="wf-home">
<nav class="wf-nav">
    <a class="wf-brand" href="/"><span>W</span>WorkFlow</a>
    <ul>
        <li><a href="/">Home</a></li>
        <li><a href="/privacy">Privacy Policy</a></li>
        <li class="wf-nav-cta"><a href="/login_page">Log in or sign up</a></li>
    </ul>
</nav>

<header class="wf-hero">
    <div class="wf-eyebrow">AI scheduling workspace</div>
    <h1>WorkFlow <em>Assistant</em></h1>
    <p>Schedules stuff so you don't have to. Plan your calendar by chatting, get advice on how to spend your time, and upload documents for extra context.</p>
    <ul class="wf-features">
        <li><a href="#demo-scheduling">AI Scheduling</a></li>
        <li><a href="#demo-consultation">Free Consultation</a></li>
        <li><a href="#demo-documents">Document Uploading</a></li>
    </ul>
</header>

<section class="wf-demo" id="demo-scheduling">
    <div>
        <div class="wf-eyebrow">AI Scheduling</div>
        <h2>Say it once. It's on your calendar.</h2>
        <p>Tell WorkFlow what you need in plain words. It finds the free time, creates the event in Google Calendar, and shows it on your week straight away.</p>
    </div>
    <div class="wf-stage">
        <div class="wf-bubble wf-user wf-step1">Block 2 hours for my essay on Wednesday afternoon</div>
        <div class="wf-slot">
            <div class="wf-bubble wf-bot wf-typing wf-step2"><i></i><i></i><i></i></div>
            <div class="wf-bubble wf-bot wf-step3">Done: <b>Essay writing</b>, Wed 2:00 to 4:00 PM.</div>
        </div>
        <div class="wf-week">
            <div class="wf-day">Mon</div><div class="wf-day">Tue</div><div class="wf-day">Wed</div><div class="wf-day">Thu</div><div class="wf-day">Fri</div>
            <div class="wf-col"><div class="wf-busy" style="top:10px;height:30px"></div></div>
            <div class="wf-col"><div class="wf-busy" style="top:60px;height:40px"></div></div>
            <div class="wf-col"><div class="wf-busy" style="top:8px;height:28px"></div><div class="wf-event">Essay writing</div></div>
            <div class="wf-col"><div class="wf-busy" style="top:30px;height:50px"></div></div>
            <div class="wf-col"><div class="wf-busy" style="top:70px;height:30px"></div></div>
        </div>
    </div>
</section>

<section class="wf-demo" id="demo-consultation">
    <div>
        <div class="wf-eyebrow">Free Consultation</div>
        <h2>Ask how to plan, not just when.</h2>
        <p>Not sure how to fit everything in? Ask. WorkFlow looks at your week and suggests a realistic plan, then schedules it if you like it.</p>
    </div>
    <div class="wf-stage">
        <div class="wf-bubble wf-user wf-step1">Finals are in two weeks. How should I study?</div>
        <div class="wf-slot">
            <div class="wf-bubble wf-bot wf-typing wf-step2"><i></i><i></i><i></i></div>
            <div class="wf-bubble wf-bot wf-step3">Here's a plan around your classes:</div>
        </div>
        <div class="wf-bubble wf-bot wf-step4">📘 Mornings: 90 minutes on your hardest subject</div>
        <div class="wf-bubble wf-bot wf-step5">🔁 Evenings: 30 minutes of review and practice tests</div>
        <div class="wf-bubble wf-bot wf-step6">📅 Want me to add these sessions to your calendar?</div>
    </div>
</section>

<section class="wf-demo" id="demo-documents">
    <div>
        <div class="wf-eyebrow">Document Uploading</div>
        <h2>Drop in a syllabus. Get your deadlines.</h2>
        <p>Attach a PDF, a screenshot, or a file from Google Drive. WorkFlow reads it and turns the dates inside into events and tasks.</p>
    </div>
    <div class="wf-stage">
        <div class="wf-file"><b>PDF</b> Biology_101_syllabus.pdf</div>
        <div class="wf-progress"><span></span></div>
        <div class="wf-row wf-step4">Lab report 1 <small>Oct 14</small></div>
        <div class="wf-row wf-step5">Midterm exam <small>Oct 28</small></div>
        <div class="wf-row wf-step6">Final project <small>Dec 5</small></div>
    </div>
</section>

<section class="wf-try" id="try">
    <h2>Ready to get your time back?</h2>
    <p>Sign in with Google to connect your calendar. It takes about a minute.</p>
    <a href="/login_page">Try WorkFlow Assistant free</a>
</section>

<div class="wf-footer">WorkFlow Assistant · AI scheduling assistant</div>
</div>
"""


def show_home_page():
    """
    Displays the WorkFlow home page: a navigation header, feature chips that
    scroll down to an animated demo of each feature, and a sign-in prompt.
    """
    st.set_page_config(page_title="WorkFlow", layout="wide")
    st.html(STYLE + PAGE)
