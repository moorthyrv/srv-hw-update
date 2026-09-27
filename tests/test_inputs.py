from fwtool.inputs import load_servers


def test_bom_blank_rows_extra_columns_duplicates(tmp_path):
    p = tmp_path / "in.csv"
    p.write_bytes(
        "﻿Name,BMC_IP,support,site,environment,owner\n"
        "web01,10.0.0.1,HPE,LON,prod,teamA\n"
        "\n"
        ",,,,,\n"
        "db01, 10.0.0.2 ,tpm,NYC,nonprod,teamB\n"
        "web01-dup,10.0.0.1,,,,\n"
        "noip,,,,,\n"
        "bad,10.0.0.0/24,,,,\n".encode("utf-8")
    )
    servers, rep = load_servers(p)
    assert [s.bmc_ip for s in servers] == ["10.0.0.1", "10.0.0.2"]
    assert servers[0].support == "hpe" and servers[1].support == "tpm"
    assert servers[0].extra == {"owner": "teamA"}
    assert rep.blank == 2 and rep.missing_ip == 1
    assert len(rep.duplicates) == 1 and rep.invalid_ip == ["10.0.0.0/24"]


def test_minimal_columns_and_aliases(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("hostname;ip;vendor\nh1;10.1.1.1;HP\nh2;10.1.1.2;Dell EMC\n", encoding="utf-8")
    servers, _ = load_servers(p)
    assert [(s.name, s.bmc_ip, s.vendor) for s in servers] == [("h1", "10.1.1.1", "hpe"), ("h2", "10.1.1.2", "dell")]


def test_missing_ip_column(tmp_path):
    import pytest

    p = tmp_path / "in.csv"
    p.write_text("name,address\nx,1.2.3.4\n")
    with pytest.raises(ValueError):
        load_servers(p)
