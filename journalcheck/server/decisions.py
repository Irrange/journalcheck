"""Conservative final-decision recognition; queue names are not decisions."""
import re


def terminal_reason(status: str) -> str | None:
    value = ' '.join((status or '').casefold().split()).strip(' .。')
    # Some platforms append the decision date to the displayed status.
    value = re.sub(r'\s*\((?:\d{1,2}[- ]\w+[- ]\d{4}|\d{4}-\d{2}-\d{2})\)$', '', value)
    if value in {
        'you need to choose another journal', 'choose another journal',
        'transfer offered', 'transfer recommended', 'transfer suggested',
        'recommended for transfer', 'reject and transfer', 'reject with transfer',
        'rejected with transfer', 'reject with transfer option',
        '建议转投', '拒稿并建议转投', '拒稿后转投',
    } or re.fullmatch(r'(?:immediate )?reject(?:ed)? (?:and|with) transfer(?: to .+)?', value):
        return '拒稿／建议转投'
    if value in {
        'reject', 'rejected', 'manuscript rejected', 'submission rejected',
        'your submission has been rejected', 'your manuscript has been rejected',
        'decline', 'declined', 'immediate reject', 'immediate reject outright',
        'desk reject', 'desk rejected', 'reject without review', 'rejected without review',
        'reject after review', 'rejected after review', '拒稿', '已拒稿',
    }:
        return '拒稿'
    if value in {'accept', 'accepted', 'accepted for publication', 'manuscript accepted',
                 'submission accepted', 'your submission has been accepted',
                 'your manuscript has been accepted', '录用', '已录用'}:
        return '已录用'
    if value in {'withdraw', 'withdrawn', 'manuscript withdrawn', 'submission withdrawn',
                 'your submission has been withdrawn', '撤稿', '已撤稿'}:
        return '已撤回'
    if value in {'published', 'production completed', '已发表'}:
        return '已发表'
    # Decision in Process / With a Decision / Completed may include revisions.
    # Reviewer invitations and transfers in progress do not establish a decision.
    return None
