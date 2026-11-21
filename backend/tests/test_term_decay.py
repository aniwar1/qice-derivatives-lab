from app.term_decay import black_price, daily_decay


def test_same_forward_and_volatility_one_day_loss_matches_repricing():
    f, strike, vol = 1200, 1220, 0.23
    for side in ("P", "C"):
        near = black_price(f, strike, vol * (30 / 365) ** 0.5, side)
        far = black_price(f, strike, vol * (90 / 365) ** 0.5, side)
        loss = daily_decay(f, strike, near, side, 30)
        assert (
            abs(loss - (near - black_price(f, strike, vol * (29 / 365) ** 0.5, side)))
            < 1e-8
        )
        assert loss > daily_decay(f, strike, far, side, 90)
    assert daily_decay(100, 90, 5, "C", 30) is None
