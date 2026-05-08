import logging
import os
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from requests_tracker import storage
from requests_tracker.storage import CookiesFileStorage

from hargreaves import account, config, session
from hargreaves.authentication.clients import AuthenticationClient
from hargreaves.utils.logs import LogHelper


def patch_login_step_three(logger):
    original_login = AuthenticationClient.login

    def login_with_step_three(self, web_session, cfg, redirect_response=None):
        response = original_login(self, web_session, cfg, redirect_response)
        if 'login-step-three' not in response.url:
            return response

        verification_code = os.getenv('HL_VERIFICATION_CODE', '').strip()
        if verification_code:
            logger.info('Using HL verification code provided via environment variable.')
        else:
            logger.warning('HL requested text verification. Enter the code from your SMS below.')
            if not sys.stdin.isatty():
                raise ValueError(
                    'HL requested text verification, but no interactive input is available. '
                    'Set HL_VERIFICATION_CODE and retry.'
                )
            verification_code = input('HL verification code: ').strip()
        if not verification_code:
            raise ValueError('No verification code entered for login-step-three')

        soup = BeautifulSoup(response.text, 'html.parser')
        forms = soup.find_all('form')
        form = None
        best_score = -999
        for candidate in forms:
            action = (candidate.get('action') or '').lower()
            score = 0

            if 'login-step-three' in action:
                score += 10
            if '/my-accounts' in action:
                score += 5
            if '/search' in action:
                score -= 20

            for field in candidate.find_all('input'):
                field_name = (field.get('name') or '').lower()
                field_id = (field.get('id') or '').lower()
                field_type = (field.get('type') or '').lower()
                if field_type in ('text', 'tel', 'number', 'password'):
                    score += 1
                if any(token in field_name or token in field_id for token in ('verify', 'verification', 'code', 'otp', 'sms', 'two-factor')):
                    score += 5

            if score > best_score:
                best_score = score
                form = candidate

        if form is None:
            raise ValueError('Could not find verification form in login-step-three response')

        action = form.get('action') or response.url
        post_url = urljoin(response.url, action)

        body = {}
        code_field_name = None
        fallback_text_field_name = None
        for field in form.find_all('input'):
            name = field.get('name')
            if not name:
                continue
            input_type = (field.get('type') or 'text').lower()
            value = field.get('value') or ''
            field_id = (field.get('id') or '').lower()
            field_name_lower = name.lower()

            if input_type in ('hidden', 'submit'):
                body[name] = value
                continue

            if input_type in ('text', 'tel', 'number', 'password'):
                if fallback_text_field_name is None:
                    fallback_text_field_name = name

                if any(token in field_name_lower or token in field_id for token in ('verify', 'verification', 'code', 'otp', 'sms', 'two-factor')):
                    code_field_name = name
                    body[name] = verification_code
                    continue

            if value:
                body[name] = value

        if code_field_name is None and fallback_text_field_name is not None:
            code_field_name = fallback_text_field_name
            body[code_field_name] = verification_code

        if code_field_name is None:
            raise ValueError('Could not detect verification code field on login-step-three form')

        logger.debug(f"Submitting verification form to {post_url} using field '{code_field_name}'")
        verify_response = web_session.post(post_url, data=body)
        if 'login-step-three' in verify_response.url:
            raise ValueError('Verification code was not accepted. Please retry with a fresh code.')
        return verify_response

    AuthenticationClient.login = login_with_step_three

if __name__ == '__main__':

    logger = LogHelper.configure(logging.DEBUG)
    patch_login_step_three(logger)

    # load api config from secrets.json if present, otherwise fall back to env vars
    secrets_path = Path(__file__).parent.joinpath('secrets.json')
    if secrets_path.exists():
        config = config.load_api_config(str(secrets_path))
    else:
        config = config.load_api_config()
    # create logged-in web session (+ load previous cookies file):
    session_cache_path = Path(__file__).parent.joinpath('session_cache')
    session_cache_path.mkdir(parents=True, exist_ok=True)
    cookies_storage = CookiesFileStorage(session_cache_path)
    web_session = session.create_session(cookies_storage, config)
    write_har = os.getenv('HL_WRITE_HAR', '0') == '1'

    try:
        accounts = account.get_account_summary(web_session=web_session)
        for account_summary in accounts:
            # Fetches information in my-accounts page
            print(account_summary)

        snapshot_time = datetime.now(timezone.utc)
        snapshot_id = snapshot_time.strftime('%Y-%m-%d_%H-%M-%S')
        snapshot_file = session_cache_path.joinpath(f'session-{snapshot_id}.json')

        all_holdings = set()
        account_holdings = {}
        snapshot = {
            'snapshot_id': snapshot_id,
            'snapshot_time_utc': snapshot_time.isoformat(),
            'source': 'hargreaves-sdk-python',
            'notes': {
                'purchase_time': 'Not provided by this SDK account holdings endpoint',
                'transaction_history': 'Not provided by this SDK account holdings endpoint'
            },
            'accounts': []
        }

        for account_summary in accounts:
            # Fetches information in my-accounts page
            account_detail = account.get_account_detail(web_session=web_session, account_summary=account_summary)
            print(f'Your {account_detail.account_type} is worth {account_detail.total_value} with the following '
                  f'holdings:')

            account_key = f'{account_detail.account_type} ({account_summary.account_id})'
            account_holdings[account_key] = []
            account_snapshot = {
                'account_id': account_summary.account_id,
                'account_type': account_detail.account_type,
                'stock_value_gbp': account_detail.stock_value,
                'total_cash_gbp': account_detail.total_cash,
                'amount_available_to_invest_gbp': account_detail.amount_available,
                'total_value_gbp': account_detail.total_value,
                'investments': []
            }

            for investment in account_detail.investments:
                print(f'\tYou hold {investment.units_held} units of {investment.security_name} '
                      f'worth {investment.value_gbp}')

                security_name = (investment.security_name or '').strip()
                if security_name:
                    all_holdings.add(security_name)
                    account_holdings[account_key].append(security_name)

                account_snapshot['investments'].append({
                    'stock_ticker': investment.stock_ticker,
                    'security_name': investment.security_name,
                    'sedol_code': investment.sedol_code,
                    'units_held': investment.units_held,
                    'price_pence': investment.price_pence,
                    'value_gbp': investment.value_gbp,
                    'cost_gbp': investment.cost_gbp,
                    'gain_loss_gbp': investment.gain_loss_gbp,
                    'gain_loss_percentage': investment.gain_loss_percentage,
                    'purchase_time': None,
                    'purchase_time_note': 'Not available from current SDK holdings endpoint'
                })

            snapshot['accounts'].append(account_snapshot)

        print('\nUnique holdings across all your HL accounts:')
        for security_name in sorted(all_holdings):
            print(f'- {security_name}')

        print(f'\nTotal unique holdings: {len(all_holdings)}')

        print('\nHoldings by account:')
        for account_key, holdings in account_holdings.items():
            print(f'- {account_key}: {len(set(holdings))} unique holdings')

        snapshot['unique_holdings'] = sorted(all_holdings)
        snapshot['unique_holdings_count'] = len(all_holdings)

        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        print(f'\nSnapshot written to {snapshot_file}')

    except Exception as ex:
        if 'List of accounts not present in response' in str(ex):
            logger.error(
                'Login appears incomplete: HL redirected away from account summary. '
                'This can happen when HL asks for an extra verification step not handled by the SDK. '
                'Try logging into HL in your browser first, then re-run this script. '
                'If needed, delete session_cache/cookies.txt and retry.'
            )
        logger.error(traceback.format_exc())
    finally:
        # persist cookies to local file
        cookies_storage.save(web_session.cookies)

        if write_har:
            try:
                # writes to 'session-cache/session-DD-MM-YYYY HH-MM-SS.har' file
                storage.write_HAR_to_local_file(session_cache_path, web_session.request_session_context)
                # converts HAR file to markdown + response files in folder 'session-cache/session-DD-MM-YYYY HH-MM-SS/'
                storage.convert_HAR_to_markdown(session_cache_path, web_session.request_session_context)
            except UnicodeEncodeError:
                logger.warning('Skipping HAR export due to Windows encoding issue (cp1252).')
            except Exception:
                logger.warning('Skipping HAR export due to unexpected export error.')