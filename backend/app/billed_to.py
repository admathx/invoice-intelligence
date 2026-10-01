"""Noticing an invoice that belongs to another restaurant.

An invoice goes in wherever it's uploaded or forwarded, whoever it's
addressed to: an owner with two restaurants forwards one's invoice to the
other's address, a bookkeeper picks the wrong location. Its spending and
prices then count for the wrong restaurant, and nothing ever said so.

The name printed on it can't simply be compared with the location's name:
distributors bill a legal entity ("HP Hospitality LLC") at least as often as
the name over the door, so that would hold most real invoices. It is held
only on evidence:

- it names another restaurant of the same owner and not this one; or
- this distributor's earlier invoices to this restaurant printed other
  names, and this one matches none of them (nor the location's own name).
  A distributor bills an account under one name, so a different one on its
  invoice is someone else's account. A new distributor using a name not
  seen before says nothing: that is how a restaurant's names are learned.
- this distributor has billed this restaurant before, and this name has a
  word in it that no name the restaurant goes by has ("Harbor Pine Kitchen
  & Bar" at Harbor & Pine Kitchen). A shorter name is an abbreviation; a
  longer one is a different business, and one in another city was filed
  here without a word.

Held, a person deletes it or says it's theirs, after which that exact name
on that distributor's invoices is theirs, whichever rule held it.
"""
import uuid
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_distributors import name_key, name_words
from app.models import Invoice, Tenant
from app.models.enums import InvoiceStatus

# Earlier invoices from the distributor, with a name on them, before an
# unfamiliar name on its next one means anything.
MIN_HISTORY = 2
# How alike two printed names must be to be one name misread or abbreviated
# ("HARBOR & PINE KITCHN").
MIN_SIMILARITY = 0.8


def same_name(a: str, b: str) -> bool:
    """Whether two names are one restaurant's: the same words, one a
    shortening of the other ("Harbor & Pine" for "Harbor & Pine Kitchen
    LLC"), or near enough to be a misreading."""
    words_a, words_b = set(name_words(a)), set(name_words(b))
    if not words_a or not words_b:
        return False
    if words_a <= words_b or words_b <= words_a:
        return True
    return SequenceMatcher(None, name_key(a), name_key(b)).ratio() >= MIN_SIMILARITY


def adds_a_word(printed: str, names: list[str]) -> bool:
    """Whether the printed name has a word that none of `names` has, nor
    anything close enough to be one misread ("KITCHN")."""
    known = {word for name in names for word in name_words(name)}
    return any(
        word not in known and not any(SequenceMatcher(None, word, other).ratio() >= MIN_SIMILARITY for other in known)
        for word in name_words(printed)
        # An initial is punctuation's doing ("H.P." for "HP"), not a word.
        if len(word) > 1 or word.isdigit()
    )


def names_sibling(printed: str, own_name: str, sibling_names: list[str]) -> bool:
    """Whether the printed name is one of the owner's other restaurants and
    not this one: it has every word that tells that restaurant's name from
    this one's, and none that tells this one's from that ("Blue Oak North"
    at Blue Oak Downtown)."""
    words, own = set(name_words(printed)), set(name_words(own_name))
    for sibling in sibling_names:
        theirs = set(name_words(sibling))
        only_theirs, only_ours = theirs - own, own - theirs
        if only_theirs and only_theirs <= words and not (only_ours & words):
            return True
    return False


def is_elsewhere(
    db: Session,
    tenant: Tenant,
    printed: str | None,
    distributor_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
) -> bool:
    """Whether an invoice printed with this customer name looks like another
    restaurant's. `distributor_id` is its distributor when that's known;
    `invoice_id` is the invoice itself, left out of its own history."""
    if not printed or not name_words(printed):
        return False
    # The names invoices here have carried: read without being held, or
    # held and then kept by a person. `usual` is this distributor's.
    carried = db.execute(
        select(Invoice.printed_customer, Invoice.distributor_id, func.count())
        .where(
            Invoice.tenant_id == tenant.id,
            Invoice.id != invoice_id,
            Invoice.printed_customer.is_not(None),
            Invoice.billed_elsewhere.is_(False),
            Invoice.status != InvoiceStatus.failed,
        )
        .group_by(Invoice.printed_customer, Invoice.distributor_id)
    ).all()
    usual = [(name, count) for name, theirs, count in carried if distributor_id is not None and theirs == distributor_id]
    # Exactly a name already theirs settles it, before anything else: an
    # owner's two restaurants on one distributor account are both billed
    # under one's name, and "It's ours" has to stick.
    if name_key(printed) in {name_key(name) for name, _ in usual}:
        return False
    if tenant.account_id is not None:
        siblings = list(
            db.scalars(select(Tenant.name).where(Tenant.account_id == tenant.account_id, Tenant.id != tenant.id))
        )
        if names_sibling(printed, tenant.name, siblings):
            return True
    if sum(count for _, count in usual) < MIN_HISTORY:
        return False
    if adds_a_word(printed, [tenant.name, *(name for name, _, _ in carried)]):
        return True
    return not same_name(printed, tenant.name) and not any(same_name(printed, name) for name, _ in usual)
