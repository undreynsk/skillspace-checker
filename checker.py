# AM 26/Oct/26 - check homeworks waiting for review against the examples from the task and accept them on approval

import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import APIRequestContext, Error, Playwright, sync_playwright

ROOT         = Path( __file__ ).parent
MEMORY_LIMIT = 64  # AM 02/Nov/26 - container memory cap in megabytes
TIME_LIMIT   = 6  # AM 02/Nov/26 - safety stop for infinite loops in student code, seconds per test; not the task limit
SCORE        = "0"
TASK_LIMIT   = 4000  # AM 26/Oct/26 - max symbols of task description
CODE_LIMIT   = 8000  # AM 26/Oct/26 - max symbols of student code shown
IMAGE        = "python:3.14-slim"
DOCKER       = [ "wsl", "-d", "Ubuntu", "-e", "docker" ]  # AM 26/Oct/26 - Docker Engine inside WSL Ubuntu, not Docker Desktop
LOG_FILE     = ROOT / "logs" / "checker.log"

load_dotenv( ROOT / ".env" )
URL       = os.getenv( "SKILLSPACE_URL", "" ).rstrip( "/" )
EMAIL     = os.getenv( "SKILLSPACE_EMAIL", "" )
PASSWORD  = os.getenv( "SKILLSPACE_PASSWORD", "" )
SCHOOL_ID = os.getenv( "SKILLSPACE_SCHOOL_ID", "" )

LIST_FILTERS = {
    "teachers": [], "courses": [], "groups": [], "lessons": [],
    "lastAnswer": { "start": None, "end": None }, "score": { "min": None, "max": None },
    "deadlineStatus": None, "status": "student_sent", "student": "",
}
DOCUMENT_QUERY = "query GetDocument($docId: ID!) { document(docId: $docId) { pages { content } } }"

# AM 26/Oct/26 - runs the solution with input() that does not print its prompt, so stdout holds only the result
# AM 02/Nov/26 - with --show-prompt (interactive run) the prompt is printed on its own line: the Far Manager console
# AM 02/Nov/26 - does not show a line without a trailing newline, so a regular input() prompt stays invisible there
RUNNER = """import builtins, runpy, sys

SHOW_PROMPT = "--show-prompt" in sys.argv

def checker_input( prompt="" ):
    if SHOW_PROMPT and prompt:
        print( prompt, flush=True )
    line = sys.stdin.readline()
    if not line:
        raise EOFError( "EOF when reading a line" )
    return line.rstrip( "\\n" )

builtins.input = checker_input
sys.argv       = [ "solution.py" ]
runpy.run_path( "solution.py", run_name="__main__" )
"""


# AM 02/Nov/26 - copies everything printed to the console into the log file; progress bar redraws (starting with \r) are skipped
class Tee( io.TextIOBase ):
    def __init__( self, console, log ):
        self.console = console
        self.log     = log

    def write( self, text: str ) -> int:
        self.console.write( text )
        if not text.startswith( "\r" ):
            self.log.write( text )
        return len( text )

    def flush( self ):
        self.console.flush()
        self.log.flush()


# AM 02/Nov/26 - first line of a Playwright error: the rest is the call log with session cookies, keep it out of the log
def short_error( error: Error ) -> str:
    return str( error ).splitlines()[ 0 ]


# AM 02/Nov/26 - input() that also logs the answer typed by the teacher; the prompt is logged by Tee
def ask( prompt: str ) -> str:
    answer = input( prompt )
    sys.stdout.log.write( answer + "\n" )
    return answer


# AM 26/Oct/26 - check Docker in WSL (WSL and dockerd start on the first call) and pull the image on the first run
def docker_prepare():
    print( "Checking Docker in WSL..." )
    if subprocess.run( DOCKER + [ "info" ], capture_output=True ).returncode != 0:
        sys.exit( "Docker in WSL Ubuntu is not available, run 'wsl -d Ubuntu -e docker info' to see why" )
    if subprocess.run( DOCKER + [ "image", "inspect", IMAGE ], capture_output=True ).returncode != 0:
        print( f"Pulling {IMAGE}..." )
        subprocess.run( DOCKER + [ "pull", IMAGE ], check=True )


# AM 26/Oct/26 - C:\Users\... -> /mnt/c/Users/..., the path dockerd in WSL sees
def wsl_path( path: str ) -> str:
    drive, rest = os.path.splitdrive( path )
    return "/mnt/" + drive[ 0 ].lower() + rest.replace( "\\", "/" )


# AM 26/Oct/26 - log in through the site form and return an API client with the session cookies
def login( playwright: Playwright ) -> APIRequestContext:
    print( "Starting browser..." )
    browser = playwright.chromium.launch()
    page    = browser.new_page()
    print( f"Opening {URL}/school/courses ..." )
    page.goto( f"{URL}/school/courses" )
    print( "Filling login form..." )
    page.get_by_text( "Вход", exact=True ).click()
    page.fill( 'input[name="emailOrLogin"]', EMAIL, timeout=15_000 )
    page.fill( 'input[name="password"]', PASSWORD )
    with page.expect_request( lambda request: "IdentityLogin" in ( request.post_data or "" ) ) as login_request:
        page.press( 'input[name="password"]', "Enter" )
    print( "Login sent, waiting for session cookie..." )
    for _ in range( 30 ):
        if any( cookie[ "name" ] == "identity-auth-token" for cookie in page.context.cookies() ):
            break
        page.wait_for_timeout( 500 )
    else:
        sys.exit( "Login failed, check .env" )

    fingerprint = json.loads( login_request.value.post_data )[ "variables" ][ "params" ][ "fingerprint" ]
    api         = playwright.request.new_context(
        base_url=URL,
        storage_state=page.context.storage_state(),
        extra_http_headers={ "school-id": SCHOOL_ID, "x-device-fingerprint": fingerprint },
    )
    browser.close()
    print( "Logged in" )
    return api


def waiting_works( api: APIRequestContext ) -> list[ dict ]:
    works, page, pages = [], 1, 1
    while page <= pages:
        response = api.get( "/api/rest/school/homework-user/list", params={
            "page": page, "filters": json.dumps( LIST_FILTERS ), "sortField": "name", "sortOrder": "asc",
        } ).json()
        works += response[ "data" ]
        pages  = response[ "pages" ]
        page  += 1
    return works


def node_text( node: dict ) -> str:
    if node.get( "type" ) == "text":
        return node[ "text" ]
    if node.get( "type" ) == "hardBreak":
        return "\n"
    text = "".join( node_text( child ) for child in node.get( "content", [] ) )
    return text + "\n" if node.get( "type" ) in ( "paragraph", "heading" ) else text


def table_rows( node: dict ):
    if node.get( "type" ) == "tableRow":
        yield [ node_text( cell ).strip() for cell in node[ "content" ] ]
    for child in node.get( "content", [] ):
        yield from table_rows( child )


# AM 26/Oct/26 - return task description and tests: the row after each "Ввод | Вывод" header
def load_task( api: APIRequestContext, document_id: str ) -> tuple[ str, list[ tuple[ str, str ] ] ]:
    response = api.post( "/api/graphql", data={ "query": DOCUMENT_QUERY, "variables": { "docId": document_id } } )
    document = ( response.json().get( "data" ) or {} ).get( "document" )
    # AM 08/Nov/26 - no document (GraphQL errors or an empty documentId): go on without the task, the teacher decides
    if not document:
        print( f"Task not loaded, documentId {document_id!r}: {response.text()[:300]}" )
        return "", []
    doc      = { "content": [ page[ "content" ] for page in document[ "pages" ] ] }
    rows     = list( table_rows( doc ) )
    tests    = [ ( rows[ i + 1 ][ 0 ], rows[ i + 1 ][ 1 ] ) for i, row in enumerate( rows[ :-1 ] ) if row == [ "Ввод", "Вывод" ] ]
    match    = re.search( r"Задача\s*\n(.+?)\n\s*Формат ввода", node_text( doc ), re.S )
    return ( match.group( 1 ).strip() if match else "" ), tests


# AM 26/Oct/26 - take the last student answer: an attached .py file or the message text
def student_code( api: APIRequestContext, work: dict ) -> str:
    answer = sorted( ( a for a in work[ "answers" ] if not a[ "teacher" ] ), key=lambda a: a[ "createdAt" ] )[ -1 ]
    for file in answer[ "files" ]:
        if file[ "name" ].endswith( ".py" ):
            return api.get( file[ "url" ] ).text()
    text = re.sub( r"<br\s*/?>|</p>", "\n", answer[ "message" ] )
    return html.unescape( re.sub( r"<[^>]+>", "", text ) ).strip()


def clip( text: str, limit: int ) -> str:
    if len( text ) <= limit:
        return text
    return text[ :limit ] + f"\ndisplayed {limit} symbols, the rest is cut"


def normalize( text: str ) -> str:
    return "\n".join( line.rstrip() for line in text.strip().splitlines() )


# AM 26/Oct/26 - each test runs in a throwaway container: no network, memory cap, read-only, solution folder mounted read-only
# AM 02/Nov/26 - tty=True attaches the teacher's keyboard and terminal for an interactive run
def sandbox_command( folder: str, *program: str, tty: bool = False ) -> list[ str ]:
    return DOCKER + [
        "run", "--rm", "-it" if tty else "-i", "--network", "none",
        "--memory", f"{MEMORY_LIMIT}m", "--memory-swap", f"{MEMORY_LIMIT}m",
        "--cpus", "1", "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp",
        "--mount", f"type=bind,source={wsl_path(folder)},target=/code,readonly", "-w", "/code",
        IMAGE, *program,
    ]


# AM 02/Nov/26 - run one test, drawing a progress bar towards TIME_LIMIT while waiting; returns stdout, stderr, exit code
def run_with_progress( command: list[ str ], data: bytes | None, label: str ) -> tuple[ str, str, int ]:
    process = subprocess.Popen( command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
    start   = time.time()
    while True:
        try:
            stdout, stderr = process.communicate( data, timeout=0.2 )
            break
        except subprocess.TimeoutExpired:
            data   = None  # AM 02/Nov/26 - input is sent on the first call only, retries just keep waiting
            filled = min( int( ( time.time() - start ) / TIME_LIMIT * 20 ), 20 )
            print( f"\r  {label} [{'#' * filled}{'.' * (20 - filled)}]", end="", flush=True )
    print( "\r" + " " * 40 + "\r", end="", flush=True )
    return stdout.decode( "utf-8", "replace" ), stderr.decode( "utf-8", "replace" ), process.returncode


# AM 26/Oct/26 - return problems found; an empty list means all tests passed with empty stderr
def run_tests( code: str, tests: list[ tuple[ str, str ] ] ) -> list[ str ]:
    problems = []
    with tempfile.TemporaryDirectory() as folder:
        ( Path( folder ) / "solution.py" ).write_text( code, encoding="utf-8" )
        ( Path( folder ) / "runner.py" ).write_text( RUNNER, encoding="utf-8" )
        command = sandbox_command( folder, "timeout", str( TIME_LIMIT ), "python", "-I", "-X", "utf8", "runner.py" )
        for number, ( stdin, expected ) in enumerate( tests, 1 ):
            # AM 26/Oct/26 - bytes, not text mode: on Windows text mode turns \n into \r\n and Linux input() keeps \r
            # AM 02/Nov/26 - extra empty line at the end: solutions reading "until an empty line" stop there, not on EOF
            stdout, stderr, returncode = run_with_progress( command, ( stdin + "\n\n" ).encode( "utf-8" ), f"test {number}" )
            # AM 02/Nov/26 - 124: the timeout utility stopped the solution after TIME_LIMIT seconds
            if returncode == 124:
                problems.append( f"test {number}: stopped after {TIME_LIMIT} s, possibly an infinite loop" )
                continue
            # AM 26/Oct/26 - 137 = 128 + 9 (SIGKILL): the kernel OOM killer stopped the container on the --memory cap
            if returncode == 137:
                problems.append( f"test {number}: killed, most likely memory limit {MEMORY_LIMIT}Mb exceeded" )
                continue
            if stderr.strip():
                problems.append( f"test {number}: stderr:\n{stderr.strip()}" )
            if normalize( stdout ) != normalize( expected ):
                problems.append( f"test {number}: input {stdin!r}\n  expected {expected!r}\n  got      {stdout.strip()!r}" )
    return problems


# AM 02/Nov/26 - run the solution with keyboard input and visible prompts, no time limit; Ctrl+C stops only this run
def run_interactive( code: str ):
    with tempfile.TemporaryDirectory() as folder:
        ( Path( folder ) / "solution.py" ).write_text( code, encoding="utf-8" )
        ( Path( folder ) / "runner.py" ).write_text( RUNNER, encoding="utf-8" )
        print( "Running student code, Ctrl+C to stop" )
        print( "-" * 80 )
        try:
            subprocess.run( sandbox_command( folder, "python", "-I", "-X", "utf8", "runner.py", "--show-prompt", tty=True ) )
        except KeyboardInterrupt:
            print( "\nStopped" )
        print( "-" * 80 )


# AM 02/Nov/26 - post a teacher answer: closed_good accepts the work, teacher_sent only comments and keeps it open
def send_answer( api: APIRequestContext, work: dict, status: str, comment: str, done: str ):
    fields = { "message": f"<p>{html.escape(comment)}</p>", "status": status, "homeworkUserId": str( work[ "id" ] ) }
    if status == "closed_good":
        fields[ "score" ] = SCORE
    # AM 02/Nov/26 - a network error (e.g. ECONNRESET) skips the work instead of crashing; if the server did not get it,
    # AM 02/Nov/26 - the work stays waiting and comes back on the next run. No retry: it could post the answer twice
    try:
        response = api.post( "/api/rest/school/homework-user/answer", multipart=fields )
    except Error as error:
        print( f"Failed: {short_error(error)}" )
        return
    print( done if response.ok else f"Failed: HTTP {response.status} {response.text()[:300]}" )


def main():
    LOG_FILE.parent.mkdir( exist_ok=True )
    log        = LOG_FILE.open( "a", encoding="utf-8", buffering=1 )  # AM 02/Nov/26 - line buffered, a crash loses nothing
    sys.stdout = Tee( sys.stdout, log )
    sys.stderr = Tee( sys.stderr, log )
    print( f"\n===== {time.strftime('%d/%b/%y %H:%M:%S')} =====" )

    if not ( URL and EMAIL and PASSWORD and SCHOOL_ID ):
        sys.exit( "Fill SKILLSPACE_URL, SKILLSPACE_SCHOOL_ID, SKILLSPACE_EMAIL and SKILLSPACE_PASSWORD in .env" )

    docker_prepare()
    try:
        check_works()
    except Error as error:
        sys.exit( f"Stopped: {short_error(error)}" )


def check_works():
    with sync_playwright() as playwright:
        api   = login( playwright )
        print( "Loading works waiting for review..." )
        works = waiting_works( api )
        print( f"Works waiting for review: {len(works)}" )

        for work in works:
            lesson = work[ "lesson" ]
            print( "\n" + "=" * 80 )
            print( f"{work['user']['name']} - {lesson['name']}" )

            print( "Loading task and student code..." )
            description, tests = load_task( api, lesson[ "documentId" ] )
            code               = student_code( api, work )
            print( f"Running {len(tests)} tests..." )
            problems = run_tests( code, tests ) if tests and code else []
            print( f"Task: {clip(description, TASK_LIMIT)}" )
            print( "-" * 80 )
            print( clip( code, CODE_LIMIT ) or "(no code in the answer)" )
            print( "-" * 80 )
            if not code:
                print( "VERDICT: no code in the answer" )
            elif not tests:
                print( "VERDICT: no examples found in the task" )
            elif problems:
                print( f"VERDICT: FAILED ({len(problems)} problems in {len(tests)} tests)" )
                print( "\n".join( problems ) )
            else:
                print( f"VERDICT: all {len(tests)} tests passed" )
                # AM 02/Nov/26 - all tests passed: accept without asking the teacher
                send_answer( api, work, "closed_good", "", "Accepted automatically" )
                continue

            while True:
                choice = ask( "[y] accept   [c] comment   [r] run interactively   [Enter] skip: " ).strip().lower()
                if choice == "r":
                    run_interactive( code )
                    continue
                if choice == "y":
                    send_answer( api, work, "closed_good", ask( "Comment (Enter for none): " ).strip(), "Accepted" )
                elif choice == "c":
                    comment = ask( "Comment: " ).strip()
                    if not comment:
                        print( "Empty comment, nothing sent" )
                        continue
                    send_answer( api, work, "teacher_sent", comment, "Comment sent, the work stays open" )
                elif choice:
                    print( f"Unknown choice: {choice!r}" )
                    continue
                break
        api.dispose()


if __name__ == "__main__":
    main()
