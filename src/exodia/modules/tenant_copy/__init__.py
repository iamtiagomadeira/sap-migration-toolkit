"""Tenant Copy methodology: HANA MDC cross-host tenant copy.

Copies a source tenant database (customer environment) into a freshly-provisioned
tenant on target machines (SAP HEC), which are later handed over to the customer.

This is the cross-host variant: source and target live on *different* HANA
systems, so the pre-checks validate both sides plus the SSL/PKI collateral that
cross-instance copy requires.
"""
