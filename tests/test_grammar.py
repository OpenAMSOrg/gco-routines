from pathlib import Path
import pytest
from lark import Lark, UnexpectedInput
from gcoroutines.frontend import control_line
PARSER=Lark((Path(__file__).parents[1]/'grammar/control-lines.lark').read_text(),parser='lalr')

@pytest.mark.parametrize('line',[
 'START','start name=a','START NAME=A_1','END','end','WAIT','WAIT ON=a',
 'wait on=a,B','START NAME=','START NAME=a b','START NAME=1','START NAME=a-b',
 'WAIT ON=','WAIT ON=a,','WAIT ON=a,,b','WAIT ON=[a,b]','WAIT ON=a, b',
 'WAIT a','END NAME=a','START\tNAME=x','WAIT\tON=a,b'])
def test_grammar_and_reference_control_parser_agree(line):
    try:PARSER.parse(line);grammar_ok=True
    except UnexpectedInput:grammar_ok=False
    node,errors=control_line(line,1)
    assert grammar_ok == (node is not None and not errors)

FULL=Lark((Path(__file__).parents[1]/'grammar/gco-routines.lark').read_text(),parser='lalr')

def full_accepts(source):
    source=source.replace('\r\n','\n').replace('\r','\n')
    if source and not source.endswith('\n'):source+='\n'
    try:FULL.parse(source);return True
    except UnexpectedInput:return False

@pytest.mark.parametrize('source',[
 '', '; comment','START\nT0\nEND\nWAIT',
 'START NAME=a\nEND\nSTART NAME=b\nWAIT ON=a\nEND\nWAIT ON=a,b',
 '  START ; comment\nG1X3Y4\n\tEND\nwait',
 'START_PRINT\nEND_PRINT\nM117 WAIT',
 'END','START\nT0','START\nSTART\nEND\nEND',
 'START NAME={x}\nEND','WAIT ON=[a]','WAIT ON=a, b','START WRONG=a\nEND',
 'N42 START NAME=a\nEND','start name=A\nend\nwait on=A',
])
def test_whole_program_grammar_agreement(source):
    from gcoroutines.frontend import parse
    assert full_accepts(source)==parse(source).ok

def test_semantics_are_not_misrepresented_as_cfg_rules():
    from gcoroutines.frontend import parse
    assert full_accepts('START NAME=default\nEND')
    assert not parse('START NAME=default\nEND').ok
    assert full_accepts('WAIT ON=a,a')
    assert not parse('WAIT ON=a,a').ok

def test_generated_syntax_corpus_agreement():
    import random
    from gcoroutines.frontend import control_line, parse
    rng=random.Random(9137)
    # Reproducible adversarial variations, not claims of formal proof.
    for _ in range(600):
        head=rng.choice(['START','start','WAIT','wait','END'])
        arg=rng.choice(['',' NAME=a',' NAME=1a',' ON=a',' ON=a,b',' ON=a, b',' ON=[a]',' NAME=a EXTRA=x'])
        text=rng.choice(['',' ','\t'])+head+arg+rng.choice(['',' ; note'])
        program=text+ ('\nEND' if head.upper()=='START' else '')
        assert full_accepts(program)==parse(program).ok, program

@pytest.mark.parametrize('source', ['WAİT ON=a', 'WAITÉ ON=a', 'START NAME=naïve\nEND',
                                    'M117 café', 'M117 text\u2028START\n',
                                    'M117 text\u0085END\n', 'start name=valid\nend'])
def test_physical_lines_and_ascii_keyword_rules(source):
    from gcoroutines.frontend import parse
    assert full_accepts(source)==parse(source).ok
