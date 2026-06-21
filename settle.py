"""Submits the MetaMask-signed EIP-3009 transfer authorization payload to the Arc Testnet chain.

Exposes `settle_authorization()` which triggers a Web3 contract transaction using standard
USDC EIP-3009 TransferWithAuthorization.
"""

from web3 import Web3
import config

# Standard USDC EIP-3009 transferWithAuthorization ABI
USDC_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "from", "type": "address"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "uint256", "name": "validAfter", "type": "uint256"},
            {"internalType": "uint256", "name": "validBefore", "type": "uint256"},
            {"internalType": "bytes32", "name": "nonce", "type": "bytes32"},
            {"internalType": "uint8", "name": "v", "type": "uint8"},
            {"internalType": "bytes32", "name": "r", "type": "bytes32"},
            {"internalType": "bytes32", "name": "s", "type": "bytes32"}
        ],
        "name": "transferWithAuthorization",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

def settle_authorization(signed_auth: dict) -> str:
    """Submits the EIP-3009 TransferWithAuthorization transaction to the USDC contract on Arc Testnet.
    
    Args:
        signed_auth: Dict containing from, to, value, validAfter, validBefore, nonce, v, r, s values.
        
    Returns:
        The transaction hash as a hex string.
    """
    private_key = config.STREAMER_PRIVATE_KEY
    if not private_key:
        raise ValueError("STREAMER_PRIVATE_KEY is not configured in the environment")

    # Format private key prefix if missing
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    # Initialize Web3
    w3 = Web3(Web3.HTTPProvider(config.ARC_RPC_URL))
    if not w3.is_connected():
        raise ConnectionError(f"Failed to connect to Arc Testnet RPC at {config.ARC_RPC_URL}")

    # Load transaction signer account
    account = w3.eth.account.from_key(private_key)
    
    # Contract setup
    usdc_contract_address = Web3.to_checksum_address(config.USDC_ARC_ADDRESS)
    usdc_contract = w3.eth.contract(address=usdc_contract_address, abi=USDC_ABI)

    # Format contract input values from the parsed payload
    from_address = Web3.to_checksum_address(signed_auth["from"])
    to_address = Web3.to_checksum_address(signed_auth["to"])
    value = int(signed_auth["value"])
    valid_after = int(signed_auth["validAfter"])
    valid_before = int(signed_auth["validBefore"])
    
    # Parse hex string parameters into byte arrays
    nonce = bytes.fromhex(signed_auth["nonce"].replace("0x", ""))
    r = bytes.fromhex(signed_auth["r"].replace("0x", ""))
    s = bytes.fromhex(signed_auth["s"].replace("0x", ""))

    # Convert v to integer dynamically (handling hex values like '1b' / '1c')
    v_val = signed_auth["v"]
    if isinstance(v_val, str):
        clean_v = v_val.replace("0x", "")
        try:
            v = int(clean_v)
        except ValueError:
            v = int(clean_v, 16)
    else:
        v = int(v_val)

    # Normalize recovery byte v to 27 or 28 if raw recovery ID is passed
    if v < 27:
        v += 27

    print(f"[settle] Formulated transaction parameters:")
    print(f"  Sender: {from_address} -> Streamer: {to_address}")
    print(f"  USDC Value: {value / 1_000_000.0} USDC (scaled: {value})")
    print(f"  Nonce: {signed_auth['nonce']}")
    
    # Fetch next transaction sequence number for the signer
    tx_nonce = w3.eth.get_transaction_count(account.address)
    
    # Build transaction function
    tx_func = usdc_contract.functions.transferWithAuthorization(
        from_address,
        to_address,
        value,
        valid_after,
        valid_before,
        nonce,
        v,
        r,
        s
    )
    
    # Estimate gas
    try:
        gas_estimate = tx_func.estimate_gas({"from": account.address})
    except Exception as e:
        print(f"[settle] Gas estimation failed, defaulting to 250k. Error: {e}")
        gas_estimate = 250000

    # Build the transaction payload
    tx_params = {
        "chainId": config.USDC_CHAIN_ID,
        "nonce": tx_nonce,
        "gas": gas_estimate + 20000,  # add buffer for safety
        "gasPrice": w3.eth.gas_price
    }
    
    tx = tx_func.build_transaction(tx_params)
    
    # Sign transaction locally using streamer's private key
    signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
    
    # Send transaction
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"[settle] Submitted transaction to Arc. Hash: {tx_hash.hex()}")
    
    # Wait for block confirmation
    print("[settle] Waiting for transaction block confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"[settle] Transaction confirmed in block {receipt['blockNumber']}")
    
    return tx_hash.hex()
