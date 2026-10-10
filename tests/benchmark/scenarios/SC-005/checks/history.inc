def verify(root):
    for key in ("AB-0007", "aB-0007", "00019", "  integration-key  ", "Tenant_A.X"):
        expect(root, {"key": key}, {"key": key})


checked_main(verify)
