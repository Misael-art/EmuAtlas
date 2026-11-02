<div align="center">

<img src="docs/business/assets/branding/emuatlas-hero.svg" alt="EmuAtlas — sua biblioteca, pronta para evoluir" width="1280" />

# EmuAtlas

**Um lugar para organizar a biblioteca, preparar o host e conectar a experiência.**

<p>
  <img alt="Versão 0.2.0-alpha.6" src="https://img.shields.io/badge/vers%C3%A3o-0.2.0--alpha.6-83f6d5?style=flat-square&amp;labelColor=101c2d" />
  <img alt="Linux x86_64" src="https://img.shields.io/badge/Linux-x86__64-6ac5fc?style=flat-square&amp;labelColor=101c2d" />
  <img alt="Python 3.10 ou superior" src="https://img.shields.io/badge/Python-3.10%2B-b9a3f5?style=flat-square&amp;labelColor=101c2d" />
  <img alt="Prévia de engenharia" src="https://img.shields.io/badge/status-pr%C3%A9via_de_engenharia-f4c786?style=flat-square&amp;labelColor=101c2d" />
</p>

<p>
  <a href="#instalar">Começar</a> &nbsp;·&nbsp;
  <a href="#ecossistema">Ecossistema</a> &nbsp;·&nbsp;
  <a href="#estrutura">Estrutura</a> &nbsp;·&nbsp;
  <a href="#estado-do-projeto">Estado do projeto</a> &nbsp;·&nbsp;
  <a href="#guias">Documentação</a>
</p>

</div>

---

Bibliotecas de emulação crescem. Jogos, artes, versões, ferramentas e configurações precisam continuar fáceis de encontrar e manter. O **EmuAtlas** reúne esse trabalho em uma estrutura única, com instalação automatizada, organização por função e resultados verificáveis.

| 🧩 Um ecossistema | 🗂️ Uma organização | 🔎 Um histórico |
| :--- | :--- | :--- |
| Frontends, emuladores, núcleos e ferramentas conectados por receitas. | ROMs, mídias, BIOS, saves e configurações com destinos definidos. | Planos, hashes, logs e resultados por componente. |

> [!IMPORTANT]
> **Prévia de engenharia.** A automação está implementada; a instalação integral em máquina limpa e a aceitação física de gameplay/controles ainda precisam de validação. As permissões de distribuição de cada componente seguem em revisão.

## Instalar

**Esta primeira publicação reúne a apresentação e a documentação.** O código-fonte e os pacotes de instalação ainda aguardam publicação revisada neste repositório. Os comandos a seguir destinam-se ao pacote de engenharia preparado localmente, em **Linux x86_64**:

```bash
# 1. Conheça o plano — sem alterar o host
./install.sh plan

# 2. Instale os componentes e prepare as configurações
./install.sh install --apply

# 3. Consulte os resultados
./install.sh status
```

**Já existe algo instalado?** O fluxo procura componentes disponíveis, reutiliza downloads íntegros e preserva personalizações. Para retomar após uma falha, execute novamente o comando de instalação; o estado real é revalidado.

Pacotes nativos podem exigir autenticação pelo **Polkit/sudo**. A inicialização não abre jogos ou interfaces dos emuladores. Em bibliotecas existentes, revise o plano antes de aplicar.

<details>
<summary><strong>Instalar um componente ou reduzir o paralelismo</strong></summary>

```bash
# Seleciona ES-DE e suas dependências declaradas
./install.sh install --components es-de --apply

# Usa um job nas compilações
./install.sh install --jobs 1 --apply
```

Um escopo parcial concluído aparece como `selected_scope_complete`. O campo `all_components_installed` só é verdadeiro quando o grafo completo termina sem falhas ou bloqueios.

Leia o [guia de instalação automatizada](docs/business/automated-install.md) para hosts suportados, retomada e limites.

</details>

## Ecossistema

<table>
  <tr>
    <td align="center"><strong>59</strong><br/>componentes no fluxo de instalação</td>
    <td align="center"><strong>24</strong><br/>núcleos libretro com receitas</td>
    <td align="center"><strong>3</strong><br/>frontends integrados ao instalador</td>
  </tr>
</table>

| Camada | O que o projeto prepara |
| :--- | :--- |
| **Frontends** | RetroFE, ES-DE e Quiver Launcher. |
| **Emulação** | RetroArch, MAME, Dolphin, DuckStation, PCSX2, RPCS3, PPSSPP, Flycast, Cemu, melonDS, Azahar e xemu. |
| **Host** | Dependências de mídia, arquivos compactados, fontes, janelas, Python, Wine e ferramentas de compilação. |
| **Organização** | Inventário, integridade, identificação, variantes, compactação e planos reversíveis. |
| **Mídia** | Ferramentas de scraping/ingestão, metadados, histórias e caminhos compartilhados por plataforma. |
| **Compilações** | Receitas para RetroFE, ZArchive, orbis-unpkg e Eden-NX2. |
| **Pesquisa** | Eden-NX2, Xenia Canary Linux, KytyPS5 e PS5PCEM permanecem experimentais. |

As receitas usam pacotes do sistema, Flatpak, arquivos verificados ou fontes fixadas por commit. **Instalar um runtime não comprova compatibilidade jogável.**

### Um fluxo que permite retomar

```mermaid
flowchart TD
    A["Planejar e diagnosticar"] --> B["Reutilizar ou instalar"]
    B --> C["Preparar configurações"]
    C --> D["Verificar e registrar"]
    D -. "Falha ou bloqueio: revalidar" .-> B
```

Tarefas independentes podem continuar quando um componente falha. Dependências pendentes ficam explícitas; o instalador retorna código **2** enquanto a seleção tiver falhas ou bloqueios. Builds têm limite de jobs e tempo; as pesadas exigem o controle de recursos declarado.

## Estrutura

```text
workspace/
├── install.sh              # Entrada única da instalação
├── tools/
│   ├── distribution/       # Bootstrap, receitas, templates e releases
│   └── roms/               # Organizadores, validação e manutenção
├── frontend/               # RetroFE, ES-DE e Quiver Launcher
├── roms/<system_id>/        # Biblioteca fornecida pelo usuário
├── media/<system_id>/       # Capas, vídeos, screenshots, histórias…
├── bios/                   # BIOS e firmware fornecidos pelo usuário
├── saves/                  # Saves e estados
├── metadata/               # Identidade e metadados locais
├── work/installation/      # Fontes, builds e cache verificado
├── outputs/installation/   # Planos, logs e resultados do instalador
└── docs/business/          # Arquitetura, operação e proposta do produto
```

**Código no repositório. Dados na instalação.** O pacote contém ferramentas, templates e documentação. Jogos, BIOS, chaves, saves, biblioteca pessoal de arte e executáveis de terceiros ficam fora; as aquisições previstas ocorrem na origem.

A mídia compartilhada possui uma origem canônica. Aliases relativos oferecem compatibilidade entre consumidores, evitando criar uma biblioteca de payloads para cada frontend.

## Estado do projeto

| Implementado | Validação que permanece necessária |
| :--- | :--- |
| Instalador, dependências e retomada por componente. | Instalação integral em máquina limpa por distribuição. |
| Configuração vazia, repetição, mídia e realocação. | Jornada visual, gameplay e controles físicos. |
| Preservação de configurações e registro de alterações. | Aceitação dos cenários de falha e retorno ao desktop por host. |
| Receitas e aquisição de componentes externos. | Licenças e insumos da oferta comercial por componente. |

**Snapshot local de validação — 02/11/2026:**

- **26** testes da distribuição e **296** regressões das ferramentas passaram.
- **10/10** verificações automáticas do RetroFE passaram.
- ZArchive foi compilado e passou por compactação/extração com hashes idênticos.
- KytyPS5, PS5PCEM e Skraper foram instalados em pastas isoladas; ferramentas Python foram instaladas e importadas em venv privado.
- O pacote extraído passou pelo preflight de mídia, contrato de Opções/saída e realocação para pasta com espaços.

Esses resultados são verificações locais registradas, **não um badge de CI remoto nem uma homologação de todos os jogos**. Veja a [matriz de qualidade e evolução](docs/business/quality-and-roadmap.md).

<details>
<summary><strong>O que depende do usuário e do host?</strong></summary>

BIOS, firmware, chaves, jogos, login Steam, contas de mídia e decisões dos assistentes upstream continuam com o usuário. Hosts imutáveis não são desbloqueados automaticamente.

O RetroFE novo recebe um fallback textual próprio. **Aeon Nox e a biblioteca pessoal de artes não acompanham o pacote.** Temas e mídias adicionais exigem origem e permissões adequadas.

As dependências estão mapeadas para Arch/BigLinux, Debian/Ubuntu e Fedora. Os pacotes disponíveis e seus requisitos podem variar; a matriz de aceitação registra os limites da prévia.

</details>

## Guias

| Quero… | Abrir |
| :--- | :--- |
| **Instalar e retomar** | [Instalação automatizada](docs/business/automated-install.md) |
| **Entender os diretórios e caminhos** | [Arquitetura](docs/business/architecture.md) |
| **Trabalhar com fontes e mídias** | [Operação e aquisição](docs/business/operations.md) |
| **Preparar a oferta do produto** | [Filosofia e modelo de negócio](docs/business/README.md) |
| **Avaliar distribuição e proveniência** | [Política de distribuição](docs/business/distribution-policy.md) |
| **Acompanhar os critérios de entrega** | [Qualidade e evolução](docs/business/quality-and-roadmap.md) |
| **Gerar e publicar um pacote** | [Releases e repositório](docs/business/releasing.md) |

## Desenvolver

Nas fontes de engenharia, quando disponibilizadas:

```bash
python3 -m unittest discover -s tools/distribution/tests
python3 tools/distribution/release.py plan
```

A allowlist `tools/distribution/config/release-files.json`, presente nas fontes de engenharia, define os arquivos publicáveis. Alterações de conteúdo seguem **inventário → validação → plano → aplicação → verificação**; a reversão depende do manifesto da operação. Não há limpeza automática da biblioteca ao iniciar o frontend.

Para relatar um problema, reúna versão do pacote, host, componente, erro e passos de reprodução. Revise os logs antes de compartilhá-los; conteúdos pessoais não são necessários para reportar um defeito do instalador.

---

<div align="center">
  <strong>Organizar com critério. Automatizar com evidência. Evoluir com consistência.</strong><br/>
  <sub>EmuAtlas · organização e automação para emulação</sub>
</div>
