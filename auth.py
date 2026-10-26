# AM 26/Oct/26 - log in to Skillspace in a browser, save the session and record requests for API discovery

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Response, async_playwright

ROOT       = Path( __file__ ).parent
STATE_FILE = ROOT / ".session" / "state.json"
LOG_FILE   = ROOT / "logs" / "auth_log.json"
BODY_LIMIT = 20_000
TOKEN_RE   = re.compile( r'("[^"]*token[^"]*"\s*:\s*")[^"]+', re.I )

load_dotenv( ROOT / ".env" )
URL      = os.getenv( "SKILLSPACE_URL", "" ).rstrip( "/" )
EMAIL    = os.getenv( "SKILLSPACE_EMAIL", "" )
PASSWORD = os.getenv( "SKILLSPACE_PASSWORD", "" )

entries = []
tasks   = []


def redact( text: str | None ) -> str | None:
    if not text:
        return text
    text = text.replace( PASSWORD, "***" ).replace( EMAIL, "***" )
    return TOKEN_RE.sub( r"\1***", text )[ :BODY_LIMIT ]


# AM 26/Oct/26 - keep auth scheme and cookie names only
def mask_headers( headers: dict[ str, str ] ) -> dict[ str, str ]:
    result = {}
    for name, value in headers.items():
        if name == "cookie":
            value = "; ".join( part.split( "=" )[ 0 ].strip() for part in value.split( ";" ) )
        elif "auth" in name or "token" in name:
            value = value.split( " " )[ 0 ] + " ***"
        result[ name ] = value
    return result


async def record( response: Response ):
    try:
        body = await response.text()
    except Exception:
        body = None  # AM 26/Oct/26 - redirects have no body
    entries.append( {
        "method":  response.request.method,
        "status":  response.status,
        "url":     response.url,
        "headers": mask_headers( await response.request.all_headers() ),
        "post":    redact( response.request.post_data ),
        "body":    redact( body ),
    } )


def on_response( response: Response ):
    if response.request.resource_type in ( "xhr", "fetch", "document" ):
        tasks.append( asyncio.create_task( record( response ) ) )


async def main():
    if not ( URL and EMAIL and PASSWORD ):
        sys.exit( "Fill SKILLSPACE_URL, SKILLSPACE_EMAIL and SKILLSPACE_PASSWORD in .env" )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch( headless=False )
        context = await browser.new_context()
        context.on( "response", on_response )
        page    = await context.new_page()
        await page.goto( f"{URL}/school/courses" )
        try:
            await page.fill( 'input[type="email"], input[name*="email" i]', EMAIL, timeout=15_000 )
            await page.fill( 'input[type="password"]', PASSWORD )
            await page.press( 'input[type="password"]', "Enter" )
        except Exception:
            print( "Login form not filled automatically, log in manually" )

        await asyncio.to_thread( input, "Accept one correct work and comment on another one, then press Enter here: " )
        STATE_FILE.parent.mkdir( exist_ok=True )
        await context.storage_state( path=STATE_FILE )
        await asyncio.gather( *tasks, return_exceptions=True )
        await browser.close()

    LOG_FILE.parent.mkdir( exist_ok=True )
    LOG_FILE.write_text( json.dumps( entries, ensure_ascii=False, indent=2 ), encoding="utf-8" )
    print( f"Session saved, {len(entries)} requests logged to {LOG_FILE}" )


if __name__ == "__main__":
    asyncio.run( main() )
