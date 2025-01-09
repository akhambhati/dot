#!/bin/bash
# shellcheck disable=SC1090

case $- in
*i*) ;; # interactive
*) return ;;
esac

# Key Bindings
bind -x '"\C-l":clear'

# ---------------------- local utility functions ---------------------

_have() { type "$1" &>/dev/null; }
_source_if() { [[ -r "$1" ]] && source "$1"; }

# ----------------------- environment variables ----------------------
#                           (also see envx)

# Create XDG Directory Structure
export XDG_CONFIG_HOME="$HOME"/.config
export XDG_CACHE_HOME="$HOME"/.cache
export XDG_DATA_HOME="$HOME"/.local/share
export XDG_STATE_HOME="$HOME"/.local/state
export XDG_BIN_HOME="$HOME"/.local/bin

export LANG=en_US.UTF-8 # assuming apt install language-pack-en done
export USER="akhambhati" #"${USER:-$(whoami)}"
export GITUSER="$USER"
export REPOS="$HOME/repos"
export GHREPOS="$REPOS/github.com/$GITUSER"
export DOTFILES="$GHREPOS/dot"
export SCRIPTS="$DOTFILES/scripts"
export SNIPPETS="$DOTFILES/snippets"
export HOLOCRON="$HOME/Holocron"
export HCREPOS="$HOLOCRON/holocron-01/volume1/repos"
export CX_PATH="$HCREPOS/cortex"
export GOBIN="$XDG_BIN_HOME"
export GOPATH="$HOME/.local/go/bin"
export HELP_BROWSER=lynx
export TERM="xterm-256color"
export HRULEWIDTH=73
export EDITOR=vi
export VISUAL=vi
export EDITOR_PREFIX=vi

[[ -d /.vim/spell ]] && export VIMSPELL=("$HOME/.vim/spell/*.add")

# ----------------------------- dircolors ----------------------------

if _have dircolors; then
	if [[ -r "$HOME/.dircolors" ]]; then
		eval "$(dircolors -b "$HOME/.dircolors")"
	else
		eval "$(dircolors -b)"
	fi
fi

# ------------------------------- path -------------------------------

pathappend() {
	declare arg
	for arg in "$@"; do
		test -d "$arg" || continue
		PATH=${PATH//":$arg:"/:}
		PATH=${PATH/#"$arg:"/}
		PATH=${PATH/%":$arg"/}
		export PATH="${PATH:+"$PATH:"}$arg"
	done
} && export -f pathappend

pathprepend() {
	for arg in "$@"; do
		test -d "$arg" || continue
		PATH=${PATH//:"$arg:"/:}
		PATH=${PATH/#"$arg:"/}
		PATH=${PATH/%":$arg"/}
		export PATH="$arg${PATH:+":${PATH}"}"
	done
} && export -f pathprepend

# remember last arg will be first in path
pathprepend \
	"$XDG_BIN_HOME" \
	"$GHREPOS/cmd-"* \
	/usr/local/bin \
	"$SCRIPTS"\
	$GOPATH

pathappend \
	/usr/local/bin \
	/usr/local/sbin \
	/usr/local/games \
	/usr/games \
	/usr/sbin \
	/usr/bin \
	/sbin \
	/bin

# ------------------------------ cdpath ------------------------------

export CDPATH=".:$GHREPOS:$DOTFILES:$REPOS:$HOME"

# ------------------------ bash shell options ------------------------

# shopt is for BASHOPTS, set is for SHELLOPTS

shopt -s checkwinsize # enables $COLUMNS and $ROWS
shopt -s expand_aliases
shopt -s globstar
shopt -s dotglob
shopt -s extglob

#shopt -s nullglob # bug kills completion for some
#set -o noclobber

# -------------------------- stty annoyances -------------------------

#stty stop undef # disable control-s accidental terminal stops
stty -ixon # disable control-s/control-q tty flow control

# ------------------------------ history -----------------------------

export HISTCONTROL=ignoreboth
export HISTSIZE=5000
export HISTFILESIZE=10000

set -o vi
shopt -s histappend

# --------------------------- smart prompt ---------------------------
#                 (keeping in bashrc for portability)

function parse_git_dirty {
	[[ $(git status --porcelain 2> /dev/null) ]] && echo "*"
}

function parse_git_branch {
	git branch --no-color 2> /dev/null | sed -e '/^[^*]/d' -e "s/* \(.*\)/ (\1$(parse_git_dirty))/"
}

__ps1() {
	local P='λ ::' hn="$(hostname -f)" dir="${PWD/$HOME/"~"}" \
		B countme short long double \
		r='\[\e[31m\]' g='\[\e[37m\]' h='\[\e[34m\]' \
		u='\[\e[33m\]' p='\[\e[34m\]' w='\[\e[35m\]' \
		b='\[\e[36m\]' x='\[\e[0m\]' n='\[\e[32m\]'

	[[ $EUID == 0 ]] && P='#' && u=$r && p=$u # root

	PS1="\n$u[\D{%Y-%m-%d %H:%M:%S}] $b$hn$x:$h$dir$w$(parse_git_branch)\n$r$P$x "
}

PROMPT_COMMAND="__ps1"

# ----------------------------- keyboard -----------------------------

# only works if you have X and are using graphic Linux desktop

_have setxkbmap && test -n "$DISPLAY" &&
	setxkbmap -option caps:escape &>/dev/null

# ------------------------------ aliases -----------------------------
#      (use exec scripts instead, which work from vim and subprocs)

unalias -a

# ls
alias ls='ls --color=auto'
alias ll='ls -la'
alias la='ls -lathr'
alias path='echo -e ${PATH//:/\\n}'
alias '?'=duck
alias '??'=gpt
alias '???'=google
alias bat=batcat
alias batt='cat /sys/class/power_supply/BAT0/capacity'
alias venv='echo $(basename $VIRTUAL_ENV)'
alias fx=firefox
_have vim && alias vi=vim

# ----------------------------- functions ----------------------------

clone() {
	local repo="$1" user
	local repo="${repo#https://github.com/}"
	local repo="${repo#git@github.com:}"
	if [[ $repo =~ / ]]; then
		user="${repo%%/*}"
	else
		user="$GITUSER"
		[[ -z "$user" ]] && user="$USER"
	fi
	local name="${repo##*/}"
	local userd="$REPOS/github.com/$user"
	local path="$userd/$name"
	[[ -d "$path" ]] && cd "$path" && return
	mkdir -p "$userd"
	cd "$userd"
	echo gh repo clone "$user/$name" -- --recurse-submodule
	gh repo clone "$user/$name" -- --recurse-submodule
	cd "$name"
} && export -f clone

_source_if $GHREPOS/cortex-cli/lib/cx-utils

# ------------- source external dependencies / completion ------------

owncomp=(
	pomo
)

for i in "${owncomp[@]}"; do complete -C "$i" "$i"; done

# ------------- Python / pyenv setup ------------
export PYENV_ROOT="$HOME/.pyenv"
pathprepend \
	"$PYENV_ROOT"/bin
if [[ -f $PYENV_ROOT/bin/pyenv ]]; then
	eval "$(pyenv init -)"
fi
if [[ -f $PYENV_ROOT/versions/base/bin/activate ]]; then
	. "${PYENV_ROOT}/versions/base/bin/activate"
fi

# ------------- Cargo / Rust setup --------------
. "$HOME/.cargo/env"

# ------------- Install FZF ---
[ -f ~/.fzf.bash ] && source ~/.fzf.bash

# ------------- Start into tmux automatically ---
if command -v tmux &> /dev/null && [ -n "$PS1" ] && [[ ! "$TERM" =~ screen ]] && [[ ! "$TERM" =~ tmux ]] && [ -z "$TMUX" ]; then
  exec tmux new-session -A -s main
fi
