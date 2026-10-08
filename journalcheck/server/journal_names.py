"""Journal names from recognized page elements, with verified code fallbacks."""
import re
from journalcheck.utils import clean_text

KNOWN_SCHOLARONE = {'ageing': 'Age and Ageing'}
GENERIC = {'scholarone manuscripts', 'scholarone', 'manuscript central',
           'author dashboard', 'author center', 'author centre', 'log in', 'login',
           'submitted manuscripts', 'live manuscripts'}


def scholarone_name(code, observed=''):
    value = clean_text(observed)
    if value and value.lower() not in GENERIC and value.lower() != code.lower():
        return value
    return KNOWN_SCHOLARONE.get(code.lower(), code.upper())


def page_journal_name(soup, code):
    for tag in soup.select('#journalName, #journalTitle, .journal-name, #journal-name, #journal-title'):
        value = clean_text(tag.get_text(' ', strip=True))
        if value and value.lower() not in GENERIC and value.lower() != code.lower():
            return value
    # ScholarOne commonly puts the journal name in the banner logo, not visible text.
    for image in soup.select('img[alt], img[title]'):
        for key in ('alt', 'title'):
            value = clean_text(image.get(key, ''))
            if re.search(r'\bAge\s+and\s+Ageing\b', value, re.I) and code.lower() == 'ageing':
                return 'Age and Ageing'
    title = clean_text(soup.title.get_text(' ', strip=True)) if soup.title else ''
    if code.lower() == 'ageing' and re.search(r'\bAge\s+and\s+Ageing\b', title, re.I):
        return 'Age and Ageing'
    return scholarone_name(code)
