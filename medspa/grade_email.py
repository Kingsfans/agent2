"""Grade how much a scraped contact address can be trusted.

Addresses gathered from search results carry a specific risk: the search
summarizer returns prose, not raw page text, so it can assert an address it
constructed from the domain rather than one it read. You usually cannot tell
which happened. What you CAN test is whether the address is the kind of thing a
guesser would produce.

    A_verified     could not be constructed from what the guesser knows
    B_unconfirmed  exactly what a guesser produces -- verify before sending

A guesser knows the domain and the business name, and nothing else. So:

    info@theirdomain.com            constructible -> B
    theirbusinessname@gmail.com     constructible -> B  (freemail is not
                                    automatically strong; the local part is
                                    still just the business name)
    frontdesk@theirdomain.com       not constructible -> A
    sarah@theirdomain.com           not constructible -> A
    info@some-other-real-domain.com not constructible -> A  (knowing the other
                                    domain exists is the hard part)

That middle case is the one worth spelling out: an earlier version of this
graded every freemail address as strong, which promoted rows like
regensocietyaesthetics@gmail.com for regensocietyaesthetics.com. A model
inventing an address for that practice would write exactly that.
"""
import re

FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "pm.me", "gmx.com", "zoho.com", "comcast.net", "att.net", "verizon.net",
    "optonline.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
}
# The prefixes a guesser reaches for first.
GENERIC = {"info", "contact", "hello"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def second_level(domain):
    """Rough SLD: the label before the public suffix. Good enough to tell
    example.com from example.net, which is all this needs."""
    parts = (domain or "").lower().strip().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return parts[-3]
    return parts[0] if len(parts) == 1 else (parts[-2] if len(parts) >= 2 else "")


# A derived local part accounts for most of the name it came from. Requiring
# only "appears inside" is too loose: "sarah" sits inside
# "sarahhitchcoxaesthetics", but sarah@ is a person, not a derivation, and a
# guesser would not land on it.
COVERAGE = 0.6


def _covers(local, name):
    if not local or not name:
        return False
    if name in local:                       # local is the name plus extra
        return True
    return local in name and len(local) >= COVERAGE * len(name)


def derivable(local, domain, business=""):
    """Could this local part be written by someone who knows only the domain
    and the business name?"""
    nl = norm(local)
    if not nl:
        return True
    return _covers(nl, norm(second_level(domain))) or _covers(nl, norm(business))


def grade(email, domain, business=""):
    """Returns 'A_verified', 'B_unconfirmed', or '' for no address."""
    if not email or "@" not in email:
        return ""
    local, _, edom = email.lower().strip().partition("@")
    # .com vs .net of the same name is the same organisation for this purpose,
    # and swapping the suffix is something a guesser does readily.
    same_org = edom == domain.lower() or second_level(edom) == second_level(domain)

    if same_org and local in GENERIC:
        return "B_unconfirmed"
    if same_org or edom in FREEMAIL:
        return "B_unconfirmed" if derivable(local, domain, business) else "A_verified"
    # A different real domain: knowing it exists is not something you guess.
    return "A_verified"


if __name__ == "__main__":
    cases = [
        ("info@sculptspalv.com", "sculptspalv.com", "Sculpt Spa", "B_unconfirmed"),
        ("regensocietyaesthetics@gmail.com", "regensocietyaesthetics.com", "REGEN Society", "B_unconfirmed"),
        ("durmaesthetics@gmail.com", "durmnv.com", "DURM Aesthetics & Wellness", "B_unconfirmed"),
        ("info@drfortino.com", "drfortino.net", "Dr Fortino", "B_unconfirmed"),
        ("frontdesk@allenmedicalaesthetics.com", "allenmedicalaesthetics.com", "Allen Medical", "A_verified"),
        ("sarah@hitchcoxaesthetics.com", "hitchcoxaesthetics.com", "Sarah Hitchcox Aesthetics", "A_verified"),
        ("skingoods@yahoo.com", "1aesthetic.com", "1Aesthetic", "A_verified"),
        ("info@ihwoh.com", "impacthealthoh.com", "Impact Health & Wellness", "A_verified"),
        ("ssmedspa@yahoo.com", "smartskinmedspa.com", "Smart Skin Med Spa", "A_verified"),
        ("triage@coem.com", "coem.com", "Center for Occupational Medicine", "A_verified"),
        ("", "x.com", "X", ""),
    ]
    bad = 0
    for email, dom, biz, want in cases:
        got = grade(email, dom, biz)
        ok = "ok  " if got == want else "FAIL"
        bad += got != want
        print(f"  {ok} {email or '(none)':<42} -> {got or '(none)'}")
    print("all pass" if not bad else f"{bad} FAILED")
