from app.core.logging import redact


def test_phi_redaction():
    out = redact("Pt SSN 123-45-6789 DOB 03/14/1985 call (217) 555-0142 jane@x.com MRN: 00482913")
    assert "123-45-6789" not in out and "03/14/1985" not in out
    assert "555-0142" not in out and "jane@x.com" not in out and "00482913" not in out
