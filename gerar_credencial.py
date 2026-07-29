"""
Gerador de credenciais — Triagem de Execuções Fiscais
HERA Tecnologia / PGMS

Emite o par (segredo, hash) para a API e para o painel. O SEGREDO aparece uma
única vez, na tela, e não é gravado em lugar nenhum — quem precisar dele que
copie agora. O que vai para o ambiente do serviço é sempre o HASH.

É essa separação que dá o ganho: variável de ambiente não é cofre — o valor
aparece na UI do Easypanel, em `docker inspect`, em /proc/<pid>/environ e em
dump de erro. Guardando só o hash, quem ler qualquer um desses caminhos não
consegue autenticar.

Uso:
    # Token para um consumidor da API (SIAP, por exemplo)
    python gerar_credencial.py api siap

    # Senha do painel — gera uma forte automaticamente
    python gerar_credencial.py painel

    # Senha do painel — usando uma que você escolheu
    python gerar_credencial.py painel "minha senha"
"""

import sys
import hashlib
import secrets

PREFIXO   = "pgms_live_"

# Parâmetros do scrypt para a senha do painel. Senha humana tem entropia baixa
# e é atacável por dicionário, então aqui o hash PRECISA ser lento de propósito.
# Para os tokens da API é o contrário: 256 bits aleatórios não têm força bruta
# possível, e um SHA-256 direto basta — hash lento ali não compraria nada.
SCRYPT_N  = 16384
SCRYPT_R  = 8
SCRYPT_P  = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024


def _linha(c="─", n=68):
    print(c * n)


def gerar_token_api(rotulo: str):
    if not rotulo or ":" in rotulo or "," in rotulo:
        sys.exit("Rótulo inválido — use algo simples como 'siap', sem ':' nem ','.")

    token = PREFIXO + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()

    _linha("═")
    print(f"  TOKEN DA API — consumidor '{rotulo}'")
    _linha("═")
    print()
    print("  1. ENTREGUE ESTE SEGREDO AO CONSUMIDOR (aparece só agora):")
    print()
    print(f"     {token}")
    print()
    print("     Use um cofre com link de uso único. Nunca e-mail ou WhatsApp —")
    print("     os dois guardam histórico indefinidamente, fora do seu controle.")
    print()
    _linha()
    print()
    print("  2. CONFIGURE ISTO NO AMBIENTE DO SERVIÇO (Easypanel → Ambiente):")
    print()
    print(f"     API_TOKENS={rotulo}:sha256:{digest}")
    print()
    print("     Para vários consumidores, separe por vírgula.")
    print("     Para ROTACIONAR sem derrubar a integração, deixe os dois hashes")
    print(f"     com o mesmo rótulo — '{rotulo}:sha256:<novo>,{rotulo}:sha256:<antigo>' —")
    print("     avise o consumidor, espere a troca e só então remova o antigo.")
    print()
    _linha("═")


def gerar_senha_painel(senha: str = None):
    gerada = senha is None
    if gerada:
        # 4 blocos legíveis — forte e ainda digitável por uma pessoa
        senha = "-".join(secrets.token_urlsafe(6) for _ in range(4))

    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        senha.encode(), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32,
    )
    guardado = f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"

    _linha("═")
    print("  SENHA DO PAINEL DO PROCURADOR")
    _linha("═")
    print()
    if gerada:
        print("  1. SENHA GERADA (aparece só agora — guarde num gerenciador):")
        print()
        print(f"     {senha}")
        print()
    else:
        print("  1. Senha informada por você — guarde num gerenciador de senhas.")
        print()
    _linha()
    print()
    print("  2. CONFIGURE ISTO NO AMBIENTE DO SERVIÇO (Easypanel → Ambiente):")
    print()
    print(f"     SENHA_PAINEL_HASH={guardado}")
    print()
    print("     Remova a variável SENHA_PAINEL antiga, se existir — enquanto ela")
    print("     estiver lá, a senha continua em texto puro no ambiente.")
    print()
    _linha("═")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("api", "painel"):
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "api":
        if len(sys.argv) < 3:
            sys.exit("Informe o rótulo do consumidor. Ex.: python gerar_credencial.py api siap")
        gerar_token_api(sys.argv[2])
    else:
        gerar_senha_painel(sys.argv[2] if len(sys.argv) > 2 else None)


if __name__ == "__main__":
    main()
