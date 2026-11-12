# Instruções de manutenção do produto

- Leia README.md e docs/business/ antes de modificar escopo, distribuição ou estrutura.
- Código reutilizável e templates são fontes; dados do cliente nunca pertencem ao repositório.
- Use tools/distribution/release.py para exportar a allowlist; nunca publicar uma biblioteca instalada inteira.
- Use o organizador existente em tools/roms/ e seus planos, locks e reversão. Não criar limpeza autônoma no boot.
- Novos componentes precisam origem, tamanho, hash, requisitos, licença e teste no host alvo. Não inferir direitos de distribuição de acesso gratuito.
- Testes de instalação precisam pastas temporárias, nenhuma GUI, nenhuma mudança em controles ou pacotes do host. Gameplay e retorno ao desktop exigem teste físico separado.
- Não exportar ROM, BIOS, chaves, saves, histórico pessoal, tokens ou arte sem licença. Manter esta prévia privada enquanto a matriz de prontidão tiver bloqueios.
- As decisões e evidências de cada mudança ficam no histórico local do operador; o repositório contém documentação genérica e testes sem conteúdo pessoal.
