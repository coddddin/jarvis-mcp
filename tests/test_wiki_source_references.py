import pytest
from jarvis.app.wiki_routes import resolve_draft,source_quotes

def test_quotes_are_copied_without_spacing_or_unicode_changes():
    text='로컬 Ollama로 추론한다.\n지식은 사용자가 검토한 뒤 저장한다.'
    quotes=source_quotes(text)
    result=resolve_draft({'summary':'요약','facts':[{'text':'사실','quote_id':1}]},quotes)
    assert result['facts'][0]['quote']==text

@pytest.mark.parametrize('reference',[0,2,True,'1',None])
def test_unknown_or_non_integer_reference_is_refused(reference):
    with pytest.raises(ValueError):
        resolve_draft({'summary':'요약','facts':[{'text':'사실','quote_id':reference}]},{1:'원본'})

def test_generated_quote_is_not_accepted_alongside_reference():
    with pytest.raises(ValueError):
        resolve_draft({'summary':'요약','facts':[{'text':'사실','quote_id':1,'quote':'변형'}]},{1:'원본'})

def test_all_chunks_are_exact_bounded_source_substrings():
    source='가나다 로컬 Ollama\n'*700
    quotes=source_quotes(source)
    assert ''.join(quotes.values())==source
    assert all(0<len(value)<=400 for value in quotes.values())
