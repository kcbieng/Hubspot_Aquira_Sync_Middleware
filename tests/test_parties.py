from app.mapping.parties import link_account_advertiser, party_type_for_client


def test_party_type_rules():
    account = {"ID": 101, "IsAccount": True, "IsAdvertiser": False}
    advertiser = {"ID": 202, "IsAccount": False, "IsAdvertiser": True}
    both = {"ID": 303, "IsAccount": True, "IsAdvertiser": True}

    assert party_type_for_client(account) == "account"
    assert party_type_for_client(advertiser) == "advertiser"
    assert party_type_for_client(both) == "both"


def test_party_type_and_parent_links_handle_fieldvalue_wrappers():
    account = {"ID": {"Value": 101}, "IsAccount": {"Value": True}, "IsAdvertiser": {"Value": False}}
    advertiser = {"ID": {"Value": 202}, "IsAccount": {"Value": False}, "IsAdvertiser": {"Value": True}}
    both = {"ID": {"Value": 303}, "IsAccount": {"Value": True}, "IsAdvertiser": {"Value": True}}

    assert party_type_for_client(account) == "account"
    assert party_type_for_client(advertiser) == "advertiser"
    assert party_type_for_client(both) == "both"

    linked = link_account_advertiser(account, advertiser)
    assert linked == {"account_id": 101, "advertiser_id": 202, "needs_parent": True}
